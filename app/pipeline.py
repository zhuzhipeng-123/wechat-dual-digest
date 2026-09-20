from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from app.core import (
    AccountConfig,
    Article,
    Candidate,
    DigestReport,
    StageError,
    StageStatus,
    TaskKind,
    deduplicate_candidates,
    freeze_window,
    practice_title_matches,
)
from app.reporting import ReportPublisher


class Discoverer(Protocol):
    def discover(self, account: AccountConfig) -> list[Candidate]: ...


class Fetcher(Protocol):
    def fetch(self, url: str, expected_account: str = "") -> Article: ...


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
) -> DigestReport:
    window = freeze_window(target_date, schedule)
    report = DigestReport(
        run_id=new_run_id(kind, generated_at),
        kind=kind,
        target_date=target_date.isoformat(),
        window=window,
        generated_at=generated_at,
        source_status=StageStatus.EMPTY,
        analysis_status=StageStatus.NOT_RUN
        if kind == "practice"
        else StageStatus.NEEDS_VERIFICATION,
    )
    source_succeeded = 0
    for account in accounts:
        try:
            candidates = deduplicate_candidates(discoverer.discover(account))
            source_succeeded += 1
        except Exception as error:
            report.errors.append(
                StageError("discovery", type(error).__name__, str(error), account.name)
            )
            continue
        for candidate in candidates:
            if candidate.account != account.name:
                report.errors.append(
                    StageError(
                        "discovery",
                        "account_mismatch",
                        "搜索结果公众号名不是精确匹配",
                        account.name,
                    )
                )
                continue
            if kind == "practice" and not practice_title_matches(candidate.title, account):
                continue
            try:
                article = fetcher.fetch(candidate.url, expected_account=account.name)
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
            article_payload = asdict(article)
            if article.publish_time is None:
                article_payload["reason"] = "原文精确时间未知"
                report.pending.append(article_payload)
                continue
            if not window.contains(article.publish_time):
                continue
            if kind == "recruitment":
                if analyzer is None:
                    article_payload["companies"] = []
                    article_payload["analysis_status"] = StageStatus.NEEDS_VERIFICATION
                else:
                    try:
                        article_payload["companies"] = analyzer(article)
                        article_payload["analysis_status"] = StageStatus.OK
                        report.analysis_status = StageStatus.OK
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
    if source_succeeded == len(accounts):
        report.source_status = (
            StageStatus.OK if report.articles or report.pending else StageStatus.EMPTY
        )
    elif source_succeeded:
        report.source_status = StageStatus.PARTIAL
    else:
        report.source_status = StageStatus.FAILED
    if kind == "recruitment" and analyzer is not None and not report.articles and not report.errors:
        report.analysis_status = StageStatus.EMPTY
    return report


def publish_preview(report: DigestReport, project_root: Path) -> tuple[Path, Path]:
    return ReportPublisher(project_root / "templates", project_root / "output").publish(
        report, preview=True
    )
