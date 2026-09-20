from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.agnes import (
    AgnesAuthenticationError,
    AgnesClient,
    AgnesConfigurationError,
    AgnesResponseError,
    AgnesSettings,
)
from app.core import (
    ConfigurationError,
    UnsafeUrlError,
    freeze_window,
    is_safe_image_url,
    load_accounts,
    load_settings,
    safe_article_url,
)
from app.source import ArticleFetcher, ArticleFetchError, parse_article_html


def test_frozen_window_has_exact_boundaries() -> None:
    window = freeze_window(date(2026, 9, 20), "21:00")
    assert (window.end - window.start).total_seconds() == 86400
    assert window.contains(window.start)
    assert not window.contains(window.end)


def test_accounts_preserve_order_and_require_practice_keywords(tmp_path: Path) -> None:
    path = tmp_path / "accounts.toml"
    path.write_text(
        '[[recruitment]]\nname="甲"\n[[recruitment]]\nname="乙"\n'
        '[[practice]]\nname="练习"\ninclude=["每日一题"]\n',
        encoding="utf-8",
    )
    accounts = load_accounts(path)
    assert [item.name for item in accounts.recruitment] == ["甲", "乙"]
    assert accounts.practice[0].include == ("每日一题",)

    path.write_text('[[practice]]\nname="练习"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="include"):
        load_accounts(path)


def test_settings_reject_unsupported_timezone(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(
        'timezone="America/New_York"\n[recruitment]\nschedule="21:00"\n'
        '[practice]\nschedule="21:00"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Asia/Shanghai"):
        load_settings(path)


def test_article_parser_extracts_exact_time_and_sanitizes() -> None:
    raw = """
    <html><head><meta property="article:published_time" content="2026-09-20T08:30:00+08:00"></head>
    <body><h1 id="activity-name">测试文章</h1><span id="js_name">测试号</span>
    <div id="js_content"><script>alert(1)</script><p onclick="evil()">正文</p>
    <a href="javascript:alert(1)">坏链接</a>
    <img data-src="https://mmbiz.qpic.cn/example/image.jpg"></div></body></html>
    """
    parsed = parse_article_html(raw)
    assert parsed.title == "测试文章"
    assert parsed.account == "测试号"
    assert parsed.publish_time == datetime(2026, 9, 20, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert parsed.publish_time_basis == "meta:article:published_time"
    assert "script" not in parsed.sanitized_html
    assert "onclick" not in parsed.sanitized_html
    assert "javascript:" not in parsed.sanitized_html
    assert parsed.image_urls == ("https://mmbiz.qpic.cn/example/image.jpg",)


def test_article_fetcher_allows_only_validated_wechat_redirects() -> None:
    html = """
    <meta property="article:published_time" content="2026-09-20T08:30:00+08:00">
    <h1 id="activity-name">测试</h1><span id="js_name">测试号</span>
    <div id="js_content"><p>正文</p></div>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if b"nwr_flag" not in request.url.query:
            return httpx.Response(302, headers={"location": f"{request.url}?nwr_flag=1"})
        return httpx.Response(200, text=html)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    article = ArticleFetcher(client).fetch("https://mp.weixin.qq.com/s/known-token")
    assert article.title == "测试"

    with pytest.raises(ArticleFetchError, match="公众号不匹配"):
        ArticleFetcher(client).fetch(
            "https://mp.weixin.qq.com/s/known-token", expected_account="另一个号"
        )


def test_url_policy_blocks_non_wechat_and_ip_images() -> None:
    assert (
        safe_article_url("https://mp.weixin.qq.com/s?__biz=x")
        == "https://mp.weixin.qq.com/s?__biz=x"
    )
    with pytest.raises(UnsafeUrlError):
        safe_article_url("https://example.com/s?__biz=x")
    short_url = "https://mp.weixin.qq.com/s/vMuXc7drFHRuJP-sPUpkhg"
    assert safe_article_url(short_url) == short_url
    signed_url = "https://mp.weixin.qq.com/s?src=11&timestamp=1789919770&signature=verified"
    assert safe_article_url(signed_url) == signed_url
    assert is_safe_image_url("https://mmbiz.qpic.cn/example.jpg")
    assert not is_safe_image_url("https://127.0.0.1/example.jpg")


def test_agnes_client_enforces_fixed_response_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://apihub.agnes-ai.com/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "model": "another-model",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            },
        )

    client = AgnesClient(AgnesSettings(api_key="test"), transport=httpx.MockTransport(handler))
    with pytest.raises(AgnesResponseError, match="不匹配"):
        client.complete_json([{"role": "user", "content": "synthetic"}])


def test_agnes_missing_key_and_failure_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    with pytest.raises(AgnesConfigurationError):
        AgnesSettings.from_environment()

    def authentication_failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "hidden"})

    client = AgnesClient(
        AgnesSettings(api_key="test"), transport=httpx.MockTransport(authentication_failure)
    )
    with pytest.raises(AgnesAuthenticationError):
        client.complete_json([{"role": "user", "content": "synthetic"}])


def test_agnes_rejects_truncated_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "agnes-2.5-flash",
                "choices": [{"message": {"content": '{"partial":'}, "finish_reason": "length"}],
            },
        )

    client = AgnesClient(AgnesSettings(api_key="test"), transport=httpx.MockTransport(handler))
    with pytest.raises(AgnesResponseError, match="截断"):
        client.complete_json([{"role": "user", "content": "synthetic"}])
