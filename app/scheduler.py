from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from app.core import DeliveryStatus, FrozenWindow, TaskKind, freeze_window

_SECRET_KEY_PARTS = ("password", "secret", "token", "cookie", "api_key", "feed_url")


def _safe_snapshot(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if not key.casefold().endswith("_env")
            and any(part in key.casefold() for part in _SECRET_KEY_PARTS)
            else _safe_snapshot(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_safe_snapshot(item) for item in value]
    return value


@dataclass(frozen=True)
class RunClaim:
    run_id: str
    kind: TaskKind
    target_date: str
    window_start: str
    window_end: str
    config_json: str
    process_id: int
    claimed_at: str


@dataclass(frozen=True)
class ScheduledTask:
    kind: TaskKind
    schedule: str
    config_snapshot: dict
    catch_up_today: bool = False


class RunLedger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    process_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT,
                    UNIQUE(kind, target_date, window_start, window_end)
                )
                """
            )
            self._ensure_column(connection, "runs", "report_html", "TEXT")
            self._ensure_column(connection, "runs", "report_json", "TEXT")
            self._ensure_column(
                connection,
                "runs",
                "delivery_status",
                "TEXT NOT NULL DEFAULT 'not_requested'",
            )
            self._ensure_column(
                connection, "runs", "delivery_attempts", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(connection, "runs", "delivery_error", "TEXT")
            self._ensure_column(connection, "runs", "delivered_at", "TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    kind TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    article_key TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    publish_time TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_run_id TEXT,
                    error TEXT,
                    PRIMARY KEY(kind, account_key, article_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    kind TEXT PRIMARY KEY,
                    active_schedule TEXT NOT NULL,
                    pending_schedule TEXT,
                    pending_effective_date TEXT
                )
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def claim(
        self,
        kind: TaskKind,
        target_date: date,
        window: FrozenWindow,
        config_snapshot: dict,
        run_id: str,
        now: datetime,
        process_id: int | None = None,
    ) -> RunClaim | None:
        process_id = process_id or os.getpid()
        values = (
            run_id,
            kind,
            target_date.isoformat(),
            window.start.isoformat(),
            window.end.isoformat(),
            json.dumps(_safe_snapshot(config_snapshot), ensure_ascii=False, sort_keys=True),
            process_id,
            "claimed",
            now.isoformat(),
        )
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO runs (
                    run_id, kind, target_date, window_start, window_end, config_json,
                    process_id, status, claimed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return None
        return RunClaim(*values[:7], values[8])

    def mark_started(self, run_id: str, now: datetime) -> None:
        self._transition(run_id, "claimed", "running", now, "started_at")

    def mark_completed(self, run_id: str, now: datetime) -> None:
        self._transition(run_id, "running", "completed", now, "finished_at")

    def mark_failed(self, run_id: str, now: datetime, error: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE runs SET status='failed', finished_at=?, error=?
                WHERE run_id=? AND status IN ('claimed','running')""",
                (now.isoformat(), error[:2000], run_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"运行状态不可转为 failed：{run_id}")

    def _transition(
        self, run_id: str, expected: str, target: str, now: datetime, timestamp_column: str
    ) -> None:
        if timestamp_column not in {"started_at", "finished_at"}:
            raise ValueError("invalid timestamp column")
        with self._connect() as connection:
            changed = connection.execute(
                f"UPDATE runs SET status=?, {timestamp_column}=? WHERE run_id=? AND status=?",
                (target, now.isoformat(), run_id, expected),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"运行状态不可从 {expected} 转为 {target}：{run_id}")

    def get(self, run_id: str) -> dict | None:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_for_date(self, target_date: date) -> list[dict]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM runs WHERE target_date=? ORDER BY kind",
                (target_date.isoformat(),),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_report(
        self,
        run_id: str,
        html_path: Path,
        json_path: Path,
        delivery_status: DeliveryStatus,
    ) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE runs SET report_html=?, report_json=?, delivery_status=?
                WHERE run_id=? AND status='running'""",
                (str(html_path), str(json_path), delivery_status.value, run_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"无法记录运行报告：{run_id}")

    def begin_delivery(self, run_id: str, *, force: bool = False) -> bool:
        allowed = ("pending", "failed") if not force else ("pending", "failed", "sent", "unknown")
        placeholders = ",".join("?" for _ in allowed)
        with self._connect() as connection:
            changed = connection.execute(
                f"""UPDATE runs SET delivery_status='sending',
                delivery_attempts=delivery_attempts+1,
                delivery_error=NULL WHERE run_id=? AND report_html IS NOT NULL
                AND delivery_status IN ({placeholders})""",
                (run_id, *allowed),
            ).rowcount
        return changed == 1

    def finish_delivery(
        self, run_id: str, status: DeliveryStatus, now: datetime, message: str
    ) -> None:
        if status not in {DeliveryStatus.SENT, DeliveryStatus.FAILED, DeliveryStatus.UNKNOWN}:
            raise ValueError("delivery terminal status required")
        delivered_at = now.isoformat() if status == DeliveryStatus.SENT else None
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE runs SET delivery_status=?, delivery_error=?, delivered_at=?
                WHERE run_id=? AND delivery_status='sending'""",
                (status.value, message[:1000], delivered_at, run_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"无法完成发送状态：{run_id}")

    def is_collected(self, kind: TaskKind, account_key: str, article_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM articles WHERE kind=? AND account_key=? AND article_key=?
                AND status='collected'""",
                (kind, account_key, article_key),
            ).fetchone()
        return row is not None

    def mark_seen(
        self,
        kind: TaskKind,
        account_key: str,
        article_key: str,
        source_url: str,
        now: datetime,
        *,
        publish_time: str | None = None,
        status: str = "seen",
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO articles (
                    kind, account_key, article_key, source_url, publish_time,
                    first_seen_at, last_seen_at, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, account_key, article_key) DO UPDATE SET
                    source_url=excluded.source_url,
                    publish_time=COALESCE(excluded.publish_time, articles.publish_time),
                    last_seen_at=excluded.last_seen_at,
                    status=CASE WHEN articles.status='collected'
                        THEN articles.status ELSE excluded.status END,
                    error=excluded.error""",
                (
                    kind,
                    account_key,
                    article_key,
                    source_url,
                    publish_time,
                    now.isoformat(),
                    now.isoformat(),
                    status,
                    error[:1000] if error else None,
                ),
            )

    def mark_collected(
        self,
        kind: TaskKind,
        account_key: str,
        article_key: str,
        source_url: str,
        publish_time: str,
        run_id: str,
        now: datetime,
    ) -> None:
        self.mark_seen(
            kind,
            account_key,
            article_key,
            source_url,
            now,
            publish_time=publish_time,
            status="ready",
        )
        with self._connect() as connection:
            connection.execute(
                """UPDATE articles SET status='collected', report_run_id=?, error=NULL
                WHERE kind=? AND account_key=? AND article_key=?""",
                (run_id, kind, account_key, article_key),
            )

    def effective_schedule(self, kind: TaskKind, configured: str, today: date) -> str:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM schedules WHERE kind=?", (kind,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schedules(kind, active_schedule) VALUES (?, ?)",
                    (kind, configured),
                )
                connection.execute("COMMIT")
                return configured
            active = row["active_schedule"]
            pending = row["pending_schedule"]
            effective = row["pending_effective_date"]
            if pending and effective and today >= date.fromisoformat(effective):
                active = pending
                connection.execute(
                    """UPDATE schedules SET active_schedule=?, pending_schedule=NULL,
                    pending_effective_date=NULL WHERE kind=?""",
                    (active, kind),
                )
                pending = None
            if configured == active and pending:
                connection.execute(
                    """UPDATE schedules SET pending_schedule=NULL,
                    pending_effective_date=NULL WHERE kind=?""",
                    (kind,),
                )
                pending = None
            if configured != active and configured != pending:
                connection.execute(
                    """UPDATE schedules SET pending_schedule=?, pending_effective_date=?
                    WHERE kind=?""",
                    (configured, (today + timedelta(days=1)).isoformat(), kind),
                )
            connection.execute("COMMIT")
            return active

    def list_interrupted(self) -> list[dict]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT * FROM runs WHERE status IN ('claimed','running')
                OR delivery_status='sending' ORDER BY claimed_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_completed_report(
        self,
        run_id: str,
        html_path: Path,
        json_path: Path,
        delivery_status: DeliveryStatus,
        now: datetime,
    ) -> None:
        """Explicitly reconcile a verified complete report after its old process stopped."""
        if delivery_status == DeliveryStatus.SENDING:
            delivery_status = DeliveryStatus.UNKNOWN
        with self._connect() as connection:
            changed = connection.execute(
                """UPDATE runs SET status='completed', finished_at=?, report_html=?, report_json=?,
                delivery_status=?, delivery_error=CASE WHEN ?='unknown'
                    THEN '发送过程曾中断，结果待人工核对' ELSE delivery_error END
                WHERE run_id=? AND status IN ('claimed','running','failed')""",
                (
                    now.isoformat(),
                    str(html_path),
                    str(json_path),
                    delivery_status.value,
                    delivery_status.value,
                    run_id,
                ),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"运行不满足显式恢复条件：{run_id}")


def due_in_natural_minute(now: datetime, schedule: str) -> bool:
    return now.strftime("%H:%M") == schedule


def due_today(now: datetime, schedule: str, catch_up_today: bool) -> bool:
    if due_in_natural_minute(now, schedule):
        return True
    return catch_up_today and now.strftime("%H:%M") > schedule


def schedule_takes_effect(config_changed_on: date, target_date: date) -> bool:
    return target_date > config_changed_on


class DailyScheduler:
    """Claim every due task first, then execute with a bounded worker pool."""

    def __init__(
        self,
        ledger: RunLedger,
        runner: Callable[[RunClaim], None],
        max_workers: int = 2,
    ) -> None:
        self.ledger = ledger
        self.runner = runner
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="digest")
        self.futures: set[Future[None]] = set()

    def tick(self, now: datetime, tasks: tuple[ScheduledTask, ...]) -> list[RunClaim]:
        due = [task for task in tasks if due_today(now, task.schedule, task.catch_up_today)]
        claims: list[RunClaim] = []
        for task in due:
            window = freeze_window(now.date(), task.schedule)
            run_id = f"{task.kind}-{now:%Y%m%dT%H%M}-{uuid.uuid4().hex[:8]}"
            claim = self.ledger.claim(
                task.kind,
                now.date(),
                window,
                task.config_snapshot,
                run_id,
                now,
            )
            if claim:
                claims.append(claim)
        for claim in claims:
            future = self.executor.submit(self._execute, claim, now)
            self.futures.add(future)
            future.add_done_callback(self.futures.discard)
        return claims

    def _execute(self, claim: RunClaim, claimed_at: datetime) -> None:
        self.ledger.mark_started(claim.run_id, claimed_at)
        try:
            self.runner(claim)
        except Exception as error:
            self.ledger.mark_failed(claim.run_id, datetime.now(claimed_at.tzinfo), str(error))
            return
        self.ledger.mark_completed(claim.run_id, datetime.now(claimed_at.tzinfo))

    def close(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait, cancel_futures=False)
