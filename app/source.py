from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from app.core import (
    Article,
    ImageEvidence,
    StageStatus,
    is_safe_image_url,
    safe_article_url,
)

MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_TIME_PATTERNS = (
    (re.compile(r"\bvar\s+ct\s*=\s*[\"'](?P<value>\d{10})[\"']"), "script:ct"),
    (re.compile(r"\bcreate_time\s*[:=]\s*[\"']?(?P<value>\d{10})"), "script:create_time"),
    (re.compile(r"\bpublish_time\s*[:=]\s*[\"']?(?P<value>\d{10})"), "script:publish_time"),
)


class ArticleFetchError(RuntimeError):
    pass


class ImageDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedArticle:
    title: str
    account: str
    publish_time: datetime | None
    publish_time_basis: str | None
    sanitized_html: str
    text: str
    image_urls: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryProbeResult:
    status: StageStatus
    page_url: str
    page_title: str
    authenticated: bool
    public_account_capability: bool
    evidence: str


def _parse_publish_time(soup: BeautifulSoup, raw_html: str) -> tuple[datetime | None, str | None]:
    zone = ZoneInfo("Asia/Shanghai")
    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if isinstance(meta, Tag) and meta.get("content"):
        try:
            parsed = datetime.fromisoformat(str(meta["content"]).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone)
            return parsed.astimezone(zone), "meta:article:published_time"
        except ValueError:
            pass
    for pattern, basis in _TIME_PATTERNS:
        match = pattern.search(raw_html)
        if match:
            return datetime.fromtimestamp(int(match.group("value")), tz=zone), basis
    return None, None


def _sanitize_content(content: Tag) -> tuple[str, str, tuple[str, ...]]:
    for blocked in content.select("script, style, iframe, object, embed, video, audio, form"):
        blocked.decompose()
    image_urls = []
    for node in content.find_all(True):
        for attribute in list(node.attrs):
            if attribute.lower().startswith("on") or attribute.lower() in {"style", "srcset"}:
                del node.attrs[attribute]
        if node.name == "a":
            try:
                node["href"] = safe_article_url(str(node.get("href", "")))
                node["rel"] = "noopener noreferrer"
            except ValueError:
                node.attrs.pop("href", None)
        elif node.name == "img":
            source = str(node.get("data-src") or node.get("src") or "")
            if is_safe_image_url(source):
                image_urls.append(source)
                node["src"] = source
            else:
                node.attrs.pop("src", None)
            node.attrs.pop("data-src", None)
        else:
            node.attrs.pop("href", None)
            node.attrs.pop("src", None)
    text = "\n".join(line.strip() for line in content.get_text("\n").splitlines() if line.strip())
    return str(content), html.unescape(text), tuple(dict.fromkeys(image_urls))


def parse_article_html(raw_html: str) -> ParsedArticle:
    soup = BeautifulSoup(raw_html, "html.parser")
    content = soup.select_one("#js_content")
    if not isinstance(content, Tag):
        raise ArticleFetchError("原文缺少 #js_content，可能需要验证或页面结构已变化")
    title_node = soup.select_one("#activity-name")
    account_node = soup.select_one("#js_name")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    account = account_node.get_text(" ", strip=True) if account_node else ""
    if not title:
        meta_title = soup.find("meta", attrs={"property": "og:title"})
        title = str(meta_title.get("content", "")).strip() if isinstance(meta_title, Tag) else ""
    publish_time, basis = _parse_publish_time(soup, raw_html)
    sanitized_html, text, image_urls = _sanitize_content(content)
    return ParsedArticle(
        title or "标题待核实",
        account or "公众号待核实",
        publish_time,
        basis,
        sanitized_html,
        text,
        image_urls,
    )


class ArticleFetcher:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(20, connect=10),
            headers={"User-Agent": "Mozilla/5.0 WeChatDualDigest/0.1"},
        )

    def fetch(self, url: str, expected_account: str = "") -> Article:
        safe_url = safe_article_url(url)
        try:
            current_url = safe_url
            for _ in range(4):
                response = self.client.get(current_url)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = urljoin(current_url, response.headers.get("location", ""))
                    current_url = safe_article_url(location)
                    continue
                response.raise_for_status()
                break
            else:
                raise ArticleFetchError("原文重定向次数超过限制")
            parsed = parse_article_html(response.text)
        except (httpx.HTTPError, ArticleFetchError) as error:
            raise ArticleFetchError(f"原文读取失败：{type(error).__name__}: {error}") from error
        article_id = hashlib.sha256(safe_url.encode()).hexdigest()[:20]
        account = (
            parsed.account
            if parsed.account != "公众号待核实"
            else expected_account or parsed.account
        )
        if expected_account and account != expected_account:
            raise ArticleFetchError(f"原文公众号不匹配：期望 {expected_account}，实际 {account}")
        return Article(
            article_id,
            account,
            parsed.title,
            safe_url,
            parsed.publish_time,
            parsed.publish_time_basis,
            parsed.sanitized_html,
            parsed.text,
            StageStatus.OK if parsed.publish_time else StageStatus.NEEDS_VERIFICATION,
        )


def download_image(
    client: httpx.Client, url: str, destination_dir: Path, evidence_id: str
) -> ImageEvidence:
    if not is_safe_image_url(url):
        return ImageEvidence(evidence_id, url, None, None, StageStatus.FAILED, "图片 URL 不安全")
    try:
        current = url
        for _ in range(3):
            response = client.get(current, follow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                current = response.headers.get("location", "")
                if not is_safe_image_url(current):
                    raise ImageDownloadError("图片重定向目标不安全")
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            suffix = ALLOWED_CONTENT_TYPES.get(content_type)
            if suffix is None:
                raise ImageDownloadError(f"不支持的图片类型：{content_type or '未知'}")
            body = response.content
            if not body or len(body) > MAX_IMAGE_BYTES:
                raise ImageDownloadError("图片为空或超过 12 MiB")
            digest = hashlib.sha256(body).hexdigest()
            destination_dir.mkdir(parents=True, exist_ok=True)
            path = destination_dir / f"{evidence_id}-{digest[:12]}{suffix}"
            path.write_bytes(body)
            return ImageEvidence(evidence_id, url, path.name, digest, StageStatus.OK)
        raise ImageDownloadError("图片重定向次数超过限制")
    except (httpx.HTTPError, ImageDownloadError, OSError) as error:
        return ImageEvidence(evidence_id, url, None, None, StageStatus.FAILED, str(error))


def image_path_is_valid(path: Path, expected_sha256: str) -> bool:
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256


class WeReadDiscoveryProbe:
    """Inspect the current WeRead UI without guessing a hidden article selector."""

    def __init__(self, profile_dir: Path, channel: str = "chrome") -> None:
        self.profile_dir = profile_dir
        self.channel = channel

    def run(self) -> DiscoveryProbeResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError("缺少 Playwright；请先安装锁定依赖") from error
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir), channel=self.channel, headless=False
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=45_000)
                body = page.locator("body").inner_text(timeout=10_000)
                authenticated = "登录" not in body[:1200]
                capability = any(marker in body for marker in ("公众号", "搜一搜", "微信文章"))
                status = (
                    StageStatus.OK
                    if authenticated and capability
                    else StageStatus.NEEDS_LOGIN
                    if not authenticated
                    else StageStatus.NEEDS_VERIFICATION
                )
                evidence = (
                    "当前页面可访问，且观察到公众号发现入口。"
                    if capability
                    else "当前页面未观察到公众号发现入口；没有把电子书搜索当作公众号发现。"
                )
                return DiscoveryProbeResult(
                    status, page.url, page.title(), authenticated, capability, evidence
                )
            finally:
                context.close()
