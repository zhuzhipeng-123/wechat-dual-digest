from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.core import FrozenWindow, TaskKind, freeze_window


@dataclass(frozen=True)
class RunClaim:
    run_id: str
    kind: TaskKind
    target_date: str
    window_start: str
    window_end: str
    config_json: str
    process_id: int


@dataclass(frozen=True)
class ScheduledTask:
    kind: TaskKind
    schedule: str
    config_snapshot: dict


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
            json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True),
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
        return RunClaim(*values[:7])

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


def due_in_natural_minute(now: datetime, schedule: str) -> bool:
    return now.strftime("%H:%M") == schedule


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
        due = [task for task in tasks if due_in_natural_minute(now, task.schedule)]
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
