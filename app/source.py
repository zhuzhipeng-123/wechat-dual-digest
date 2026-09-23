from __future__ import annotations

import hashlib
import html
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from app.core import (
    AccountConfig,
    Article,
    Candidate,
    ImageEvidence,
    StageStatus,
    article_identity,
    is_safe_image_url,
    safe_article_url,
)

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_RUN_IMAGE_BYTES = 80 * 1024 * 1024
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ARTICLE_BYTES = 8 * 1024 * 1024
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


class SourceConfigurationError(RuntimeError):
    pass


class SourceAuthenticationError(RuntimeError):
    pass


class SourceFormatError(RuntimeError):
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


_ALLOWED_TAGS = {
    "div",
    "section",
    "p",
    "br",
    "span",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "blockquote",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "sup",
    "sub",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "img",
}


def _sanitize_content(content: Tag) -> tuple[str, str, tuple[str, ...]]:
    for blocked in content.select(
        "script, style, iframe, object, embed, video, audio, form, input, button, svg, math"
    ):
        blocked.decompose()
    image_urls = []
    for node in [content, *list(content.find_all(True))]:
        if node is not content and node.name not in _ALLOWED_TAGS:
            node.unwrap()
            continue
        allowed_attributes: set[str] = set()
        if node.name == "a":
            allowed_attributes = {"href", "rel"}
        elif node.name == "img":
            allowed_attributes = {"src", "data-src", "alt", "title"}
        elif node.name in {"td", "th"}:
            allowed_attributes = {"colspan", "rowspan"}
        for attribute in list(node.attrs):
            if attribute.lower() not in allowed_attributes:
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


def _response_bytes(response: httpx.Response, limit: int, label: str) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise SourceFormatError(f"{label}超过 {limit // (1024 * 1024)} MiB 上限")
    return bytes(body)


def _same_feed_origin(source: str, target: str) -> bool:
    left, right = urlparse(source), urlparse(target)
    return (
        right.scheme == "https"
        and left.scheme == right.scheme
        and left.hostname == right.hostname
        and left.port == right.port
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in node:
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


class FeedDiscoverer:
    """Consume one explicitly configured RSS/Atom source; original pages remain authoritative."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(20, connect=10),
            headers={"User-Agent": "WeChatDualDigest/0.2"},
        )
        self.now = now or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self.environment = environment if environment is not None else os.environ

    def discover(self, account: AccountConfig) -> list[Candidate]:
        if not account.enabled:
            return []
        if not account.feed_url_env:
            raise SourceConfigurationError(f"{account.name} 未配置 feed_url_env")
        feed_url = self.environment.get(account.feed_url_env, "").strip()
        parsed_url = urlparse(feed_url)
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            raise SourceConfigurationError(
                f"{account.name} 的 {account.feed_url_env} 必须是 HTTPS 订阅地址"
            )
        body, content_type = self._get(feed_url)
        prefix = body.lstrip()[:200].lower()
        if "html" in content_type or prefix.startswith((b"<!doctype html", b"<html")):
            raise SourceAuthenticationError(f"{account.name} 的订阅源返回登录页或 HTML")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as error:
            raise SourceFormatError(f"{account.name} 的订阅源不是合法 RSS/Atom") from error
        root_name = _local_name(root.tag)
        if root_name == "rss":
            return self._parse_rss(root, account)
        if root_name == "feed":
            return self._parse_atom(root, account)
        raise SourceFormatError(f"{account.name} 的订阅源根节点不是 RSS/Atom")

    def _get(self, feed_url: str) -> tuple[bytes, str]:
        current = feed_url
        for _ in range(4):
            try:
                response_context = self.client.stream("GET", current, follow_redirects=False)
            except httpx.HTTPError as error:
                raise RuntimeError(f"订阅源连接失败：{type(error).__name__}") from error
            try:
                with response_context as response:
                    if response.status_code in {401, 403}:
                        raise SourceAuthenticationError("订阅源拒绝访问，请检查授权或地址")
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(current, response.headers.get("location", ""))
                        if not _same_feed_origin(feed_url, target):
                            raise SourceConfigurationError("订阅源重定向到不同来源，已拒绝")
                        current = target
                        continue
                    try:
                        response.raise_for_status()
                    except httpx.HTTPError as error:
                        raise RuntimeError(
                            f"订阅源请求失败：HTTP {response.status_code}"
                        ) from error
                    content_type = response.headers.get("content-type", "").lower()
                    return _response_bytes(response, MAX_FEED_BYTES, "订阅源"), content_type
            except httpx.HTTPError as error:
                raise RuntimeError(f"订阅源连接失败：{type(error).__name__}") from error
        raise SourceConfigurationError("订阅源重定向次数超过限制")

    def _candidate(
        self, account_hint: str, title: str, link: str, entry_id: str | None
    ) -> Candidate | None:
        try:
            safe_url = safe_article_url(link)
        except ValueError:
            return None
        return Candidate(
            account=account_hint,
            title=title or "标题待核实",
            url=safe_url,
            discovered_at=self.now(),
            candidate_id=entry_id or article_identity(safe_url),
        )

    def _parse_rss(self, root: ET.Element, account: AccountConfig) -> list[Candidate]:
        channel = next((node for node in root if _local_name(node.tag) == "channel"), None)
        if channel is None:
            raise SourceFormatError(f"{account.name} 的 RSS 缺少 channel")
        channel_title = _child_text(channel, "title")
        result: list[Candidate] = []
        for item in channel:
            if _local_name(item.tag) != "item":
                continue
            candidate = self._candidate(
                _child_text(item, "author", "creator") or channel_title,
                _child_text(item, "title"),
                _child_text(item, "link"),
                _child_text(item, "guid") or None,
            )
            if candidate:
                result.append(candidate)
        return result

    def _parse_atom(self, root: ET.Element, account: AccountConfig) -> list[Candidate]:
        feed_title = _child_text(root, "title")
        result: list[Candidate] = []
        for entry in root:
            if _local_name(entry.tag) != "entry":
                continue
            link = ""
            author = ""
            for child in entry:
                name = _local_name(child.tag)
                if name == "link" and child.attrib.get("rel", "alternate") == "alternate":
                    link = child.attrib.get("href", "")
                elif name == "author":
                    author = _child_text(child, "name")
            candidate = self._candidate(
                author or feed_title,
                _child_text(entry, "title"),
                link,
                _child_text(entry, "id") or None,
            )
            if candidate:
                result.append(candidate)
        return result


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

    def fetch(self, url: str, expected_account: str = "", asset_dir: Path | None = None) -> Article:
        safe_url = safe_article_url(url)
        try:
            current_url = safe_url
            for _ in range(4):
                with self.client.stream("GET", current_url, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = urljoin(current_url, response.headers.get("location", ""))
                        current_url = safe_article_url(location)
                        continue
                    response.raise_for_status()
                    body = _response_bytes(response, MAX_ARTICLE_BYTES, "原文")
                    encoding = response.encoding or "utf-8"
                break
            else:
                raise ArticleFetchError("原文重定向次数超过限制")
            parsed = parse_article_html(body.decode(encoding, errors="replace"))
        except (httpx.HTTPError, ArticleFetchError, SourceFormatError) as error:
            raise ArticleFetchError(f"原文读取失败：{type(error).__name__}: {error}") from error
        article_id = article_identity(safe_url)
        account = parsed.account
        if account == "公众号待核实":
            raise ArticleFetchError("原文公众号身份缺失，不能用配置名称代替核验")
        if expected_account and parsed.account != expected_account:
            raise ArticleFetchError(f"原文公众号不匹配：期望 {expected_account}，实际 {account}")
        sanitized_html = parsed.sanitized_html
        images: list[ImageEvidence] = []
        if asset_dir is not None:
            replacements: dict[str, str | None] = {}
            for index, image_url in enumerate(parsed.image_urls, start=1):
                evidence = download_image(self.client, image_url, asset_dir, f"img-{index:03d}")
                images.append(evidence)
                replacements[image_url] = (
                    f"assets/{evidence.relative_path}" if evidence.relative_path else None
                )
            content = BeautifulSoup(sanitized_html, "html.parser")
            for image in content.find_all("img"):
                source = str(image.get("src", ""))
                replacement = replacements.get(source)
                if replacement:
                    image["src"] = replacement
                else:
                    placeholder = content.new_tag("span")
                    placeholder["class"] = "missing-image"
                    placeholder.string = "图片下载失败，请打开原文核对"
                    image.replace_with(placeholder)
            sanitized_html = str(content)
        has_failed_image = any(image.status != StageStatus.OK for image in images)
        fetch_status = (
            StageStatus.NEEDS_VERIFICATION
            if parsed.publish_time is None
            else StageStatus.PARTIAL
            if has_failed_image
            else StageStatus.OK
        )
        return Article(
            article_id,
            account,
            parsed.title,
            safe_url,
            parsed.publish_time,
            parsed.publish_time_basis,
            sanitized_html,
            parsed.text,
            fetch_status,
            tuple(images),
            "部分图片下载失败" if has_failed_image else None,
        )


def download_image(
    client: httpx.Client, url: str, destination_dir: Path, evidence_id: str
) -> ImageEvidence:
    if not is_safe_image_url(url):
        return ImageEvidence(evidence_id, url, None, None, StageStatus.FAILED, "图片 URL 不安全")
    try:
        current = url
        for _ in range(3):
            with client.stream("GET", current, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    current = urljoin(current, response.headers.get("location", ""))
                    if not is_safe_image_url(current):
                        raise ImageDownloadError("图片重定向目标不安全")
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                suffix = ALLOWED_CONTENT_TYPES.get(content_type)
                if suffix is None:
                    raise ImageDownloadError(f"不支持的图片类型：{content_type or '未知'}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_IMAGE_BYTES:
                        raise ImageDownloadError("图片超过 12 MiB")
            if not body or len(body) > MAX_IMAGE_BYTES:
                raise ImageDownloadError("图片为空或超过 12 MiB")
            digest = hashlib.sha256(body).hexdigest()
            destination_dir.mkdir(parents=True, exist_ok=True)
            existing_bytes = sum(
                path.stat().st_size for path in destination_dir.iterdir() if path.is_file()
            )
            if existing_bytes + len(body) > MAX_RUN_IMAGE_BYTES:
                raise ImageDownloadError("本次运行图片总量超过 80 MiB")
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
