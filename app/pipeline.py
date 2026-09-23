from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from app.core import (
    DEFAULT_TIMEZONE,
    AccountConfig,
    Article,
    Candidate,
    DeliveryStatus,
    DigestReport,
    StageError,
    StageStatus,
    TaskKind,
    TaskSettings,
    article_identity,
    deduplicate_candidates,
    freeze_window,
    practice_title_matches,
)
from app.reporting import ReportPublisher
from app.scheduler import RunClaim, RunLedger
from app.source import ArticleFetcher, FeedDiscoverer


class Discoverer(Protocol):
    def discover(self, account: AccountConfig) -> list[Candidate]: ...


class Fetcher(Protocol):
    def fetch(
        self, url: str, expected_account: str = "", asset_dir: Path | None = None
    ) -> Article: ...


def new_run_id(kind: TaskKind, now: datetime) -> str:
    return f"{kind}-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"


def build_report(
    *,
    kind: TaskKind,
    target_date: date,
    schedule: str,
    accounts: tuple[AccountConfig, ...],
    discoverer: Discoverer,
    fetcher: Fetcher,
    generated_at: datetime,
    analyzer: Callable[[Article], list[dict]] | None = None,
    run_id: str | None = None,
    asset_dir: Path | None = None,
    late_lookback_hours: int = 72,
    is_collected: Callable[[str, str], bool] | None = None,
    deadline: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DigestReport:
    window = freeze_window(target_date, schedule)
    report = DigestReport(
        run_id=run_id or new_run_id(kind, generated_at),
        kind=kind,
        target_date=target_date.isoformat(),
        window=window,
        generated_at=generated_at,
        source_status=StageStatus.EMPTY,
        analysis_status=StageStatus.NOT_RUN
        if kind == "practice"
        else StageStatus.NEEDS_VERIFICATION,
    )
    report.stats = {
        "accounts": len(accounts),
        "source_succeeded": 0,
        "candidates": 0,
        "fetched": 0,
        "collected_skipped": 0,
        "main_window": 0,
        "late_arrivals": 0,
        "image_ok": 0,
        "image_failed": 0,
    }
    enabled_accounts = tuple(account for account in accounts if account.enabled)
    source_succeeded = 0
    fetch_attempts = 0
    fetch_succeeded = 0
    analysis_attempts = 0
    analysis_succeeded = 0
    timed_out = False
    clock = clock or (lambda: datetime.now(window.end.tzinfo))
    late_start = window.start - timedelta(hours=late_lookback_hours)
    for account in enabled_accounts:
        if deadline is not None and clock() >= deadline:
            timed_out = True
            break
        try:
            candidates = deduplicate_candidates(discoverer.discover(account))
            source_succeeded += 1
        except Exception as error:
            report.errors.append(
                StageError("discovery", type(error).__name__, str(error), account.name)
            )
            continue
        report.stats["candidates"] += len(candidates)
        for candidate in candidates:
            if deadline is not None and clock() >= deadline:
                timed_out = True
                break
            if candidate.account and candidate.account != account.name:
                report.errors.append(
                    StageError(
                        "discovery",
                        "account_hint_mismatch",
                        "订阅源账号标签与配置不同；继续以原文身份为准",
                        account.name,
                    )
                )
            account_key = account.account_key or account.name
            candidate_key = article_identity(candidate.url)
            if is_collected is not None and is_collected(account_key, candidate_key):
                report.stats["collected_skipped"] += 1
                continue
            fetch_attempts += 1
            try:
                if asset_dir is None:
                    article = fetcher.fetch(candidate.url, expected_account=account.name)
                else:
                    article = fetcher.fetch(
                        candidate.url, expected_account=account.name, asset_dir=asset_dir
                    )
            except Exception as error:
                report.pending.append(
                    {
                        "account": account.name,
                        "title": candidate.title,
                        "url": candidate.url,
                        "reason": str(error),
                    }
                )
                report.errors.append(
                    StageError("fetch", type(error).__name__, str(error), account.name)
                )
                continue
            fetch_succeeded += 1
            report.stats["fetched"] += 1
            article_payload = asdict(article)
            if article.publish_time is None:
                article_payload["reason"] = "原文精确时间未知"
                report.pending.append(article_payload)
                continue
            if kind == "practice" and not practice_title_matches(article.title, account):
                continue
            in_main_window = window.contains(article.publish_time)
            is_late = late_start <= article.publish_time < window.start
            if not in_main_window and not is_late:
                continue
            if is_collected is not None and is_collected(account_key, article.article_id):
                report.stats["collected_skipped"] += 1
                continue
            article_payload["account_key"] = account_key
            article_payload["late_arrival"] = is_late
            report.stats["late_arrivals" if is_late else "main_window"] += 1
            report.stats["image_ok"] += sum(
                image.status == StageStatus.OK for image in article.images
            )
            report.stats["image_failed"] += sum(
                image.status != StageStatus.OK for image in article.images
            )
            if kind == "practice" and article.fetch_status == StageStatus.PARTIAL:
                article_payload["reason"] = "练习关键图片不完整，保留正文并等待后续补收"
                report.pending.append(article_payload)
                report.errors.append(
                    StageError(
                        "fetch",
                        "incomplete_images",
                        "练习文章图片不完整，未正式收录",
                        account.name,
                        article.article_id,
                    )
                )
                continue
            if kind == "recruitment":
                if analyzer is None:
                    article_payload["companies"] = []
                    article_payload["analysis_status"] = StageStatus.NEEDS_VERIFICATION
                else:
                    analysis_attempts += 1
                    try:
                        article_payload["companies"] = analyzer(article)
                        article_payload["analysis_status"] = StageStatus.OK
                        analysis_succeeded += 1
                    except Exception as error:
                        article_payload["companies"] = []
                        article_payload["analysis_status"] = StageStatus.FAILED
                        report.errors.append(
                            StageError(
                                "analysis",
                                type(error).__name__,
                                str(error),
                                account.name,
                                article.article_id,
                            )
                        )
            report.articles.append(article_payload)
        if timed_out:
            break
    if timed_out:
        report.errors.append(StageError("budget", "deadline_exceeded", "运行总预算已用尽"))
    if source_succeeded == len(enabled_accounts):
        report.source_status = (
            StageStatus.OK if report.articles or report.pending else StageStatus.EMPTY
        )
    elif source_succeeded:
        report.source_status = StageStatus.PARTIAL
    else:
        report.source_status = StageStatus.FAILED
    report.stats["source_succeeded"] = source_succeeded
    if fetch_attempts == 0:
        report.fetch_status = StageStatus.EMPTY if source_succeeded else StageStatus.NOT_RUN
    elif fetch_succeeded == fetch_attempts:
        report.fetch_status = (
            StageStatus.PARTIAL if report.stats["image_failed"] else StageStatus.OK
        )
    elif fetch_succeeded:
        report.fetch_status = StageStatus.PARTIAL
    else:
        report.fetch_status = StageStatus.FAILED
    if kind == "recruitment" and analyzer is not None:
        if analysis_attempts == 0:
            report.analysis_status = (
                StageStatus.EMPTY if not report.articles else StageStatus.NOT_RUN
            )
        elif analysis_succeeded == analysis_attempts:
            report.analysis_status = StageStatus.OK
        elif analysis_succeeded:
            report.analysis_status = StageStatus.PARTIAL
        else:
            report.analysis_status = StageStatus.FAILED
    report.coverage_status = StageStatus.NEEDS_VERIFICATION
    return report


def publish_preview(report: DigestReport, project_root: Path) -> tuple[Path, Path]:
    return ReportPublisher(project_root / "templates", project_root / "output").publish(
        report, preview=True
    )


def execute_claim(
    *,
    claim: RunClaim,
    accounts: tuple[AccountConfig, ...],
    task_settings: TaskSettings,
    ledger: RunLedger,
    project_root: Path,
    total_timeout_seconds: int,
    delivery_enabled: bool,
    deliver: Callable[[Path, DigestReport], tuple[DeliveryStatus, str]] | None = None,
    discoverer: Discoverer | None = None,
    fetcher: Fetcher | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[Path, Path]:
    """Run one already-claimed official task without inventing another run identifier."""
    now = now or (lambda: datetime.now(ZoneInfo(DEFAULT_TIMEZONE)))
    generated_at = now()
    claimed_at = datetime.fromisoformat(claim.claimed_at)
    deadline = claimed_at + timedelta(seconds=total_timeout_seconds)
    work_dir = project_root / "data" / "work" / claim.run_id
    asset_dir = work_dir / "assets"
    asset_dir.mkdir(parents=True, exist_ok=False)
    report = build_report(
        kind=claim.kind,
        target_date=date.fromisoformat(claim.target_date),
        schedule=claim.window_end[11:16],
        accounts=accounts,
        discoverer=discoverer or FeedDiscoverer(),
        fetcher=fetcher or ArticleFetcher(),
        generated_at=generated_at,
        run_id=claim.run_id,
        asset_dir=asset_dir,
        late_lookback_hours=task_settings.late_lookback_hours,
        is_collected=lambda account_key, article_key: ledger.is_collected(
            claim.kind, account_key, article_key
        ),
        deadline=deadline,
        clock=now,
    )
    report.delivery_status = (
        DeliveryStatus.PENDING if delivery_enabled else DeliveryStatus.NOT_REQUESTED
    )
    publisher = ReportPublisher(project_root / "templates", project_root / "output")
    try:
        html_path, json_path = publisher.publish(report, preview=False, asset_source_dir=asset_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    ledger.set_report(claim.run_id, html_path, json_path, report.delivery_status)
    collected_at = now()
    for article in report.articles:
        ledger.mark_collected(
            claim.kind,
            str(article["account_key"]),
            str(article["article_id"]),
            str(article["url"]),
            article["publish_time"].isoformat(),
            claim.run_id,
            collected_at,
        )
    if delivery_enabled and deliver is not None and ledger.begin_delivery(claim.run_id):
        delivery_status, message = deliver(html_path, report)
        ledger.finish_delivery(claim.run_id, delivery_status, now(), message)
    return html_path, json_path


def execute_preview(
    *,
    kind: TaskKind,
    target_date: date,
    schedule: str,
    accounts: tuple[AccountConfig, ...],
    project_root: Path,
    generated_at: datetime,
    late_lookback_hours: int,
    discoverer: Discoverer | None = None,
    fetcher: Fetcher | None = None,
) -> tuple[Path, Path]:
    run_id = new_run_id(kind, generated_at)
    work_dir = project_root / "data" / "work" / run_id
    asset_dir = work_dir / "assets"
    asset_dir.mkdir(parents=True, exist_ok=False)
    report = build_report(
        kind=kind,
        target_date=target_date,
        schedule=schedule,
        accounts=accounts,
        discoverer=discoverer or FeedDiscoverer(),
        fetcher=fetcher or ArticleFetcher(),
        generated_at=generated_at,
        run_id=run_id,
        asset_dir=asset_dir,
        late_lookback_hours=late_lookback_hours,
    )
    report.delivery_status = DeliveryStatus.NOT_REQUESTED
    try:
        return ReportPublisher(project_root / "templates", project_root / "output").publish(
            report, preview=True, asset_source_dir=asset_dir
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
