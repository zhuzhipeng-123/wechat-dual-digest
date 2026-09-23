from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.core import (
    AccountConfig,
    Article,
    Candidate,
    StageStatus,
    deduplicate_candidates,
    freeze_window,
    practice_title_matches,
)
from app.pipeline import build_report
from app.recruitment import ExtractionValidationError, rate_company, validate_extraction
from app.reporting import ReportPublisher
from app.scheduler import DailyScheduler, RunLedger, ScheduledTask

ZONE = ZoneInfo("Asia/Shanghai")


def test_title_rules_and_candidate_deduplication() -> None:
    account = AccountConfig("练习号", ("每日一题", "成语"), ("课程",))
    assert practice_title_matches("【每日 一题】第 8 期", account)
    assert not practice_title_matches("每日一题课程讲解", account)
    now = datetime(2026, 9, 20, 10, tzinfo=ZONE)
    candidates = [
        Candidate("练习号", "A", "https://mp.weixin.qq.com/s?__biz=x", now, candidate_id="same"),
        Candidate("练习号", "B", "https://mp.weixin.qq.com/s?__biz=y", now, candidate_id="same"),
    ]
    assert [item.title for item in deduplicate_candidates(candidates)] == ["A"]

    same_article = [
        Candidate("练习号", "A", "https://mp.weixin.qq.com/s/stable-token", now, "guid-1"),
        Candidate(
            "练习号",
            "A duplicate",
            "https://mp.weixin.qq.com/s/stable-token?tracking=changed",
            now,
            "guid-2",
        ),
    ]
    assert [item.title for item in deduplicate_candidates(same_article)] == ["A"]


@pytest.mark.parametrize(
    ("employer_type", "scale", "location", "expected"),
    [
        ("央国企", "大型", "北京", "P0"),
        ("互联网企业", "未确认", "杭州", "P1"),
        ("央国企", "中型", "北京", "P1"),
        ("其他企业", "大型", "上海", "P2"),
        ("性质待确认", "未确认", "天津", "P2"),
        ("性质待确认", "未确认", "西安", "P3"),
    ],
)
def test_rating_matrix(employer_type: str, scale: str, location: str, expected: str) -> None:
    jobs = [{"name": "算法工程师", "locations": [location]}]
    assert rate_company(employer_type, scale, jobs) == expected


def test_all_known_non_target_jobs_force_p3_but_unknown_does_not() -> None:
    assert rate_company("央国企", "大型", [{"name": "销售", "locations": ["北京"]}]) == "P3"
    assert rate_company("央国企", "大型", [{"name": "岗位待定", "locations": ["北京"]}]) == "P0"


def test_evidence_validation_preserves_job_location_relationship() -> None:
    source = {
        "aid": "a1",
        "source_title": "校招",
        "source_account": "就业号",
        "source_publish_time": "2026-09-20T10:00:00+08:00",
        "source_link": "https://mp.weixin.qq.com/s?__biz=x",
    }
    payload = {
        "aid": "a1",
        "is_recruitment": True,
        "is_pure_internship": False,
        "companies": [
            {
                "company": "示例公司",
                "employer_type": "性质待确认",
                "company_scale": "未确认",
                "classification_evidence_ids": [],
                "jobs": [
                    {
                        "name": "销售",
                        "locations": ["北京"],
                        "evidence_ids": ["b1"],
                        "employment_type": "校招全职",
                    },
                    {
                        "name": "算法",
                        "locations": ["郑州"],
                        "evidence_ids": ["b2"],
                        "employment_type": "校招全职",
                    },
                ],
                **{
                    key: source[key]
                    for key in (
                        "source_title",
                        "source_account",
                        "source_publish_time",
                        "source_link",
                    )
                },
            }
        ],
    }
    validated = validate_extraction(payload, source, {"b1": "北京销售", "b2": "郑州算法"})
    assert validated["companies"][0]["jobs"][1]["locations"] == ["郑州"]
    payload["companies"][0]["jobs"][1]["evidence_ids"] = ["missing"]
    with pytest.raises(ExtractionValidationError, match="不存在"):
        validate_extraction(payload, source, {"b1": "北京销售", "b2": "郑州算法"})


class FakeDiscoverer:
    def discover(self, account: AccountConfig) -> list[Candidate]:
        now = datetime(2026, 9, 20, 12, tzinfo=ZONE)
        return [Candidate(account.name, "每日一题", "https://mp.weixin.qq.com/s?__biz=x", now)]


class FakeFetcher:
    def fetch(self, url: str, expected_account: str = "") -> Article:
        return Article(
            "a1",
            expected_account,
            "每日一题",
            url,
            datetime(2026, 9, 20, 12, tzinfo=ZONE),
            "fixture",
            "<p>题目</p>",
            "题目",
            StageStatus.OK,
        )


def test_practice_pipeline_does_not_invoke_analyzer() -> None:
    called = False

    def analyzer(article: Article) -> list[dict]:
        nonlocal called
        called = True
        return []

    report = build_report(
        kind="practice",
        target_date=date(2026, 9, 20),
        schedule="21:00",
        accounts=(AccountConfig("练习号", ("每日一题",)),),
        discoverer=FakeDiscoverer(),
        fetcher=FakeFetcher(),
        generated_at=datetime(2026, 9, 20, 13, tzinfo=ZONE),
        analyzer=analyzer,
    )
    assert not called
    assert len(report.articles) == 1


def test_report_preview_is_isolated_and_escapes_external_text(tmp_path: Path) -> None:
    report = build_report(
        kind="practice",
        target_date=date(2026, 9, 20),
        schedule="21:00",
        accounts=(AccountConfig("练习号", ("每日一题",)),),
        discoverer=FakeDiscoverer(),
        fetcher=FakeFetcher(),
        generated_at=datetime(2026, 9, 20, 13, tzinfo=ZONE),
    )
    report.articles[0]["title"] = "<script>alert(1)</script>"
    publisher = ReportPublisher(Path(__file__).parents[1] / "templates", tmp_path)
    html_path, json_path = publisher.publish(report, preview=True)
    assert html_path.parent == tmp_path / "preview" / report.run_id
    assert json_path.is_file()
    html = html_path.read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_run_ledger_allows_only_one_cross_thread_claim(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite3")
    window = freeze_window(date(2026, 9, 20), "21:00")
    now = datetime(2026, 9, 20, 21, tzinfo=ZONE)

    def claim(index: int):
        return ledger.claim(
            "practice", date(2026, 9, 20), window, {"v": 1}, f"run-{index}", now, 100 + index
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    assert sum(item is not None for item in claims) == 1


def test_failed_discovery_is_not_reported_as_empty() -> None:
    class FailingDiscoverer:
        def discover(self, account: AccountConfig) -> list[Candidate]:
            raise RuntimeError("login expired")

    report = build_report(
        kind="recruitment",
        target_date=date(2026, 9, 20),
        schedule="21:00",
        accounts=(AccountConfig("招聘号"),),
        discoverer=FailingDiscoverer(),
        fetcher=FakeFetcher(),
        generated_at=datetime(2026, 9, 20, 21, tzinfo=ZONE),
    )
    assert report.source_status == StageStatus.FAILED
    assert report.errors[0].stage == "discovery"


def test_pure_internship_cannot_return_company_cards() -> None:
    source = {
        "aid": "a1",
        "source_title": "实习",
        "source_account": "就业号",
        "source_publish_time": "2026-09-20T10:00:00+08:00",
        "source_link": "https://mp.weixin.qq.com/s?__biz=x",
    }
    company = {
        "company": "示例公司",
        "employer_type": "性质待确认",
        "company_scale": "未确认",
        "classification_evidence_ids": [],
        "jobs": [],
        **{key: source[key] for key in source if key != "aid"},
    }
    payload = {
        "aid": "a1",
        "is_recruitment": True,
        "is_pure_internship": True,
        "companies": [company],
    }
    with pytest.raises(ExtractionValidationError, match="纯实习"):
        validate_extraction(payload, source, {})


def test_scheduler_claims_both_due_tasks_before_workers_and_never_reclaims(
    tmp_path: Path,
) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite3")
    observed_claim_counts: list[int] = []

    def runner(claim) -> None:
        observed_claim_counts.append(len(ledger.list_for_date(date(2026, 9, 20))))

    scheduler = DailyScheduler(ledger, runner, max_workers=2)
    now = datetime(2026, 9, 20, 21, 0, tzinfo=ZONE)
    tasks = (
        ScheduledTask("recruitment", "21:00", {"version": 1}),
        ScheduledTask("practice", "21:00", {"version": 1}),
    )
    claims = scheduler.tick(now, tasks)
    scheduler.close()
    assert {claim.kind for claim in claims} == {"recruitment", "practice"}
    assert observed_claim_counts == [2, 2]

    second_scheduler = DailyScheduler(ledger, runner, max_workers=2)
    assert second_scheduler.tick(now, tasks) == []
    assert second_scheduler.tick(now.replace(minute=1), tasks) == []
    second_scheduler.close()


def test_scheduler_records_worker_failure(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite3")

    def runner(claim) -> None:
        raise RuntimeError("synthetic failure")

    scheduler = DailyScheduler(ledger, runner, max_workers=1)
    now = datetime(2026, 9, 20, 21, 0, tzinfo=ZONE)
    claim = scheduler.tick(now, (ScheduledTask("practice", "21:00", {"version": 1}),))[0]
    scheduler.close()
    row = ledger.get(claim.run_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "synthetic failure"
