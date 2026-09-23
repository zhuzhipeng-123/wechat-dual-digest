from __future__ import annotations

import json
import shutil
import smtplib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.cli import check_configuration
from app.core import (
    AccountConfig,
    Article,
    DeliverySettings,
    DeliveryStatus,
    StageStatus,
    TaskSettings,
    article_identity,
    freeze_window,
)
from app.delivery import SMTPDelivery, build_message
from app.pipeline import build_report, execute_claim
from app.reporting import ReportPublisher
from app.scheduler import RunLedger, due_today
from app.source import (
    MAX_FEED_BYTES,
    MAX_IMAGE_BYTES,
    ArticleFetcher,
    ArticleFetchError,
    FeedDiscoverer,
    SourceAuthenticationError,
    SourceConfigurationError,
    SourceFormatError,
    download_image,
)

ZONE = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).parents[1]


def test_feed_discovery_distinguishes_items_empty_auth_and_format() -> None:
    account = AccountConfig(
        "我爱学逻辑",
        ("每日一题",),
        account_key="practice-logic",
        feed_url_env="PRACTICE_FEED_URL",
    )
    rss = """<?xml version="1.0"?><rss><channel><title>我爱学逻辑</title>
    <item><title>每日一题</title><link>https://mp.weixin.qq.com/s/token-one</link>
    <guid>entry-1</guid></item></channel></rss>""".encode()

    def response(body: bytes, content_type: str = "application/rss+xml") -> FeedDiscoverer:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=body, headers={"content-type": content_type}
            )
        )
        return FeedDiscoverer(
            httpx.Client(transport=transport),
            now=lambda: datetime(2026, 9, 21, 10, tzinfo=ZONE),
            environment={"PRACTICE_FEED_URL": "https://feed.example/practice.xml"},
        )

    candidates = response(rss).discover(account)
    assert [(item.account, item.candidate_id) for item in candidates] == [("我爱学逻辑", "entry-1")]
    assert response(b"<rss><channel><title>x</title></channel></rss>").discover(account) == []
    with pytest.raises(SourceAuthenticationError):
        response(b"<html>login</html>", "text/html").discover(account)
    with pytest.raises(SourceFormatError):
        response(b"not xml").discover(account)
    with pytest.raises(SourceConfigurationError):
        FeedDiscoverer(environment={}).discover(account)


def test_feed_redirect_must_stay_on_configured_origin() -> None:
    account = AccountConfig("号", account_key="a1", feed_url_env="FEED_URL")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "https://other.example/feed.xml"}
            )
        )
    )
    with pytest.raises(SourceConfigurationError, match="不同来源"):
        FeedDiscoverer(client, environment={"FEED_URL": "https://feed.example/x"}).discover(account)


def test_oversized_feed_stream_stops_at_limit() -> None:
    yielded = 0

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal yielded
            for _ in range(8):
                yielded += 1
                yield b"x" * (1024 * 1024)

    account = AccountConfig("号", account_key="a1", feed_url_env="FEED_URL")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/rss+xml"},
                stream=OversizedStream(),
            )
        )
    )
    with pytest.raises(SourceFormatError, match="2 MiB"):
        FeedDiscoverer(client, environment={"FEED_URL": "https://feed.example/x"}).discover(account)
    assert yielded == MAX_FEED_BYTES // (1024 * 1024) + 1


def test_article_fetch_requires_original_identity_and_rewrites_images_in_place(
    tmp_path: Path,
) -> None:
    image_url = "https://mmbiz.qpic.cn/example/picture.png"
    raw = f"""
    <meta property="article:published_time" content="2026-09-20T08:30:00+08:00">
    <h1 id="activity-name">每日一题</h1><span id="js_name">我爱学逻辑</span>
    <div id="js_content" onclick="evil()"><p>题干</p><img data-src="{image_url}">
    <p>选项</p><img data-src="{image_url}"><script>bad()</script><p>解析</p></div>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mmbiz.qpic.cn":
            return httpx.Response(200, content=b"fake-png", headers={"content-type": "image/png"})
        return httpx.Response(200, text=raw)

    fetcher = ArticleFetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    article = fetcher.fetch(
        "https://mp.weixin.qq.com/s/token-one", "我爱学逻辑", tmp_path / "assets"
    )
    assert article.fetch_status == StageStatus.OK
    assert len(article.images) == 1
    assert article.sanitized_html.count("assets/img-001-") == 2
    assert article.sanitized_html.index("题干") < article.sanitized_html.index("选项")
    assert article.sanitized_html.index("选项") < article.sanitized_html.index("解析")
    assert "onclick" not in article.sanitized_html
    assert "script" not in article.sanitized_html

    missing_account = raw.replace('<span id="js_name">我爱学逻辑</span>', "")
    missing_fetcher = ArticleFetcher(
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=missing_account))
        )
    )
    with pytest.raises(ArticleFetchError, match="身份缺失"):
        missing_fetcher.fetch("https://mp.weixin.qq.com/s/token-two", "我爱学逻辑")


def test_oversized_image_stream_stops_at_limit(tmp_path: Path) -> None:
    yielded = 0

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal yielded
            for _ in range(20):
                yielded += 1
                yield b"x" * (1024 * 1024)

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png"},
                stream=OversizedStream(),
            )
        )
    )
    result = download_image(client, "https://mmbiz.qpic.cn/example/large.png", tmp_path, "large")
    assert result.status == StageStatus.FAILED
    assert "12 MiB" in result.error
    assert yielded == MAX_IMAGE_BYTES // (1024 * 1024) + 1


class OneDiscoverer:
    def __init__(self, url: str = "https://mp.weixin.qq.com/s/token-one") -> None:
        self.url = url

    def discover(self, account: AccountConfig):
        return [
            __import__("app.core", fromlist=["Candidate"]).Candidate(
                account.name, "上游旧标题", self.url, datetime(2026, 9, 22, 10, tzinfo=ZONE)
            )
        ]


class CountingFetcher:
    def __init__(self, publish_time: datetime, title: str = "每日一题") -> None:
        self.publish_time = publish_time
        self.title = title
        self.calls = 0

    def fetch(self, url: str, expected_account: str = "", asset_dir: Path | None = None):
        self.calls += 1
        return Article(
            article_identity(url),
            expected_account,
            self.title,
            url,
            self.publish_time,
            "fixture",
            "<p>完整原文</p>",
            "完整原文",
            StageStatus.OK,
        )


def test_late_collection_original_title_and_cross_run_skip() -> None:
    url = "https://mp.weixin.qq.com/s/token-one?tracking=changed"
    fetcher = CountingFetcher(datetime(2026, 9, 21, 20, 50, tzinfo=ZONE))
    account = AccountConfig(
        "我爱学逻辑", ("每日一题",), account_key="practice-logic", feed_url_env="FEED"
    )
    report = build_report(
        kind="practice",
        target_date=date(2026, 9, 22),
        schedule="21:00",
        accounts=(account,),
        discoverer=OneDiscoverer(url),
        fetcher=fetcher,
        generated_at=datetime(2026, 9, 22, 21, tzinfo=ZONE),
        late_lookback_hours=72,
    )
    assert report.articles[0]["late_arrival"] is True
    assert report.stats["late_arrivals"] == 1

    collected = {(account.account_key, article_identity(url))}
    second_fetcher = CountingFetcher(datetime(2026, 9, 21, 20, 50, tzinfo=ZONE))
    second = build_report(
        kind="practice",
        target_date=date(2026, 9, 22),
        schedule="21:00",
        accounts=(account,),
        discoverer=OneDiscoverer(url),
        fetcher=second_fetcher,
        generated_at=datetime(2026, 9, 22, 21, tzinfo=ZONE),
        is_collected=lambda account_key, key: (account_key, key) in collected,
    )
    assert second.articles == []
    assert second_fetcher.calls == 0
    assert second.stats["collected_skipped"] == 1


def test_budget_is_checked_against_current_clock_before_discovery() -> None:
    class UnexpectedDiscoverer:
        def discover(self, account: AccountConfig):
            raise AssertionError("expired run must not start discovery")

    deadline = datetime(2026, 9, 22, 21, tzinfo=ZONE)
    report = build_report(
        kind="practice",
        target_date=date(2026, 9, 22),
        schedule="21:00",
        accounts=(AccountConfig("我爱学逻辑", ("每日一题",)),),
        discoverer=UnexpectedDiscoverer(),
        fetcher=CountingFetcher(datetime(2026, 9, 22, 12, tzinfo=ZONE)),
        generated_at=deadline - timedelta(hours=1),
        deadline=deadline,
        clock=lambda: deadline,
    )
    assert report.source_status == StageStatus.FAILED
    assert report.errors[0].code == "deadline_exceeded"


def test_configuration_check_skips_disabled_task_and_rejects_hostless_feed(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.toml").write_text(
        """
timezone = "Asia/Shanghai"
total_timeout_seconds = 300
[recruitment]
enabled = false
schedule = "21:00"
[practice]
enabled = true
schedule = "21:00"
[delivery]
enabled = false
""",
        encoding="utf-8",
    )
    (config_dir / "accounts.toml").write_text(
        """
[[practice]]
name = "我爱学逻辑"
include = ["每日一题"]
account_key = "practice-logic"
feed_url_env = "PRACTICE_FEED_URL"
""",
        encoding="utf-8",
    )
    errors, notices = check_configuration(tmp_path, {"PRACTICE_FEED_URL": "https:///missing-host"})
    assert errors == ["练习账号 我爱学逻辑：订阅地址必须使用 HTTPS"]
    assert "招聘日报：已禁用" in notices

    errors, _ = check_configuration(
        tmp_path, {"PRACTICE_FEED_URL": "https://feed.example/practice.xml"}
    )
    assert errors == []


def test_practice_missing_image_is_pending_but_recruitment_keeps_original() -> None:
    class PartialFetcher(CountingFetcher):
        def fetch(self, url: str, expected_account: str = "", asset_dir: Path | None = None):
            article = super().fetch(url, expected_account, asset_dir)
            return Article(
                **{
                    **article.__dict__,
                    "fetch_status": StageStatus.PARTIAL,
                    "error": "缺图",
                }
            )

    account = AccountConfig("我爱学逻辑", ("每日一题",))
    common = dict(
        target_date=date(2026, 9, 22),
        schedule="21:00",
        discoverer=OneDiscoverer(),
        generated_at=datetime(2026, 9, 22, 20, tzinfo=ZONE),
    )
    practice = build_report(
        kind="practice",
        accounts=(account,),
        fetcher=PartialFetcher(datetime(2026, 9, 22, 12, tzinfo=ZONE)),
        **common,
    )
    assert not practice.articles and practice.pending[0]["sanitized_html"]
    recruitment = build_report(
        kind="recruitment",
        accounts=(AccountConfig("我爱学逻辑"),),
        fetcher=PartialFetcher(datetime(2026, 9, 22, 12, tzinfo=ZONE), title="校园招聘"),
        **common,
    )
    assert recruitment.articles[0]["sanitized_html"] == "<p>完整原文</p>"


def test_report_manifest_and_fixed_entry_are_published_last(tmp_path: Path) -> None:
    report = build_report(
        kind="recruitment",
        target_date=date(2026, 9, 22),
        schedule="21:00",
        accounts=(AccountConfig("国聘"),),
        discoverer=OneDiscoverer(),
        fetcher=CountingFetcher(datetime(2026, 9, 22, 12, tzinfo=ZONE), title="校园招聘"),
        generated_at=datetime(2026, 9, 22, 21, tzinfo=ZONE),
        run_id="recruitment-fixed-run",
    )
    html_path, json_path = ReportPublisher(PROJECT_ROOT / "templates", tmp_path).publish(report)
    assert html_path.parent.name == "recruitment-fixed-run"
    assert json_path.parent == html_path.parent
    assert (html_path.parent / ".complete.json").is_file()
    fixed = tmp_path / "2026-09-22" / "招聘.html"
    assert "runs/recruitment-fixed-run/招聘.html" in fixed.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")
    assert "完整原文" in html
    assert "analysis" not in html.lower() or "needs_verification" in html


class FakeSMTP:
    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.messages = []

    def login(self, username: str, password: str) -> None:
        assert username == "user" and password == "password"

    def send_message(self, message) -> None:
        self.messages.append(message)

    def quit(self) -> None:
        pass


def test_email_is_portable_and_delivery_states_are_conservative(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "x.png").write_bytes(b"image")
    html_path = tmp_path / "report.html"
    html_path.write_text(
        '<html><body><img src="assets/x.png"><img src="assets/x.png"></body></html>',
        encoding="utf-8",
    )
    message = build_message(
        run_id="run-1",
        kind="practice",
        target_date="2026-09-22",
        status="ok",
        html_path=html_path,
        sender="from@example.com",
        recipient="to@example.com",
    )
    serialized = message.as_string()
    assert serialized.count("cid:run-1-asset-1") == 2
    assert "run-1-asset-2" not in serialized
    assert sum(part["Content-ID"] is not None for part in message.walk()) == 1
    assert str(tmp_path) not in serialized

    settings = DeliverySettings(enabled=True, host="smtp.example.com")
    environment = {
        "DIGEST_MAIL_FROM": "from@example.com",
        "DIGEST_MAIL_TO": "to@example.com",
        "DIGEST_SMTP_USER": "user",
        "DIGEST_SMTP_PASSWORD": "password",
    }
    result = SMTPDelivery(settings, environment=environment, smtp_factory=FakeSMTP).send(
        run_id="run-1",
        kind="practice",
        target_date="2026-09-22",
        report_status="ok",
        html_path=html_path,
    )
    assert result.status == DeliveryStatus.SENT

    def timeout_factory(host: str, port: int, timeout: int):
        raise TimeoutError

    unknown = SMTPDelivery(settings, environment=environment, smtp_factory=timeout_factory).send(
        run_id="run-2",
        kind="practice",
        target_date="2026-09-22",
        report_status="ok",
        html_path=html_path,
    )
    assert unknown.status == DeliveryStatus.UNKNOWN

    class RejectingSMTP(FakeSMTP):
        def login(self, username: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"rejected")

    failed = SMTPDelivery(settings, environment=environment, smtp_factory=RejectingSMTP).send(
        run_id="run-3",
        kind="practice",
        target_date="2026-09-22",
        report_status="ok",
        html_path=html_path,
    )
    assert failed.status == DeliveryStatus.FAILED
    assert "password" not in failed.message


def test_ledger_migration_collection_delivery_schedule_and_redaction(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "runs.sqlite3")
    RunLedger(tmp_path / "runs.sqlite3")  # repeated migration is safe
    now = datetime(2026, 9, 22, 22, tzinfo=ZONE)
    window = freeze_window(now.date(), "21:00")
    claim = ledger.claim(
        "practice",
        now.date(),
        window,
        {
            "feed_url": "https://secret.example/token",
            "feed_url_env": "PRACTICE_FEED_URL",
            "password": "hidden",
            "safe": 1,
        },
        "practice-ledger-run",
        now,
    )
    assert claim is not None
    row = ledger.get(claim.run_id)
    assert row is not None
    assert "secret.example" not in row["config_json"]
    snapshot = json.loads(row["config_json"])
    assert snapshot["safe"] == 1
    assert snapshot["feed_url_env"] == "PRACTICE_FEED_URL"
    assert ledger.effective_schedule("practice", "21:00", now.date()) == "21:00"
    assert ledger.effective_schedule("practice", "22:00", now.date()) == "21:00"
    assert ledger.effective_schedule("practice", "22:00", date(2026, 9, 23)) == "22:00"
    assert due_today(now, "21:00", True)
    assert not due_today(now, "23:00", True)

    ledger.mark_started(claim.run_id, now)
    html_path = tmp_path / "report.html"
    json_path = tmp_path / "report.json"
    html_path.write_text("report", encoding="utf-8")
    json_path.write_text("{}", encoding="utf-8")
    ledger.set_report(claim.run_id, html_path, json_path, DeliveryStatus.PENDING)
    assert ledger.begin_delivery(claim.run_id)
    ledger.finish_delivery(claim.run_id, DeliveryStatus.UNKNOWN, now, "uncertain")
    assert not ledger.begin_delivery(claim.run_id)
    assert ledger.begin_delivery(claim.run_id, force=True)


def test_official_claim_reuses_run_id_and_collects_only_after_complete_publish(
    tmp_path: Path,
) -> None:
    (tmp_path / "templates").mkdir()
    for template in (PROJECT_ROOT / "templates").iterdir():
        shutil.copy2(template, tmp_path / "templates" / template.name)
    ledger = RunLedger(tmp_path / "data" / "runs.sqlite3")
    now = datetime(2026, 9, 22, 21, tzinfo=ZONE)
    window = freeze_window(now.date(), "21:00")
    claim = ledger.claim("practice", now.date(), window, {"safe": True}, "practice-one-run", now)
    assert claim is not None
    ledger.mark_started(claim.run_id, now)
    url = "https://mp.weixin.qq.com/s/token-one"
    html_path, _ = execute_claim(
        claim=claim,
        accounts=(AccountConfig("我爱学逻辑", ("每日一题",), account_key="logic"),),
        task_settings=TaskSettings(True, "21:00", late_lookback_hours=72),
        ledger=ledger,
        project_root=tmp_path,
        total_timeout_seconds=300,
        delivery_enabled=False,
        discoverer=OneDiscoverer(url),
        fetcher=CountingFetcher(datetime(2026, 9, 22, 12, tzinfo=ZONE)),
        now=lambda: now,
    )
    assert html_path.parent.name == claim.run_id
    assert (html_path.parent / ".complete.json").is_file()
    row = ledger.get(claim.run_id)
    assert row is not None and row["report_html"] == str(html_path)
    assert row["delivery_status"] == DeliveryStatus.NOT_REQUESTED
    assert ledger.is_collected("practice", "logic", article_identity(url))
    ledger.recover_completed_report(
        claim.run_id,
        html_path,
        Path(row["report_json"]),
        DeliveryStatus.NOT_REQUESTED,
        now,
    )
    assert ledger.get(claim.run_id)["status"] == "completed"
