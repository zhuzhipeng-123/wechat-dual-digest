from __future__ import annotations

import hashlib
import ipaddress
import re
import tomllib
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TaskKind = Literal["recruitment", "practice"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEZONE = "Asia/Shanghai"
AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
AGNES_MODEL = "agnes-2.5-flash"
ARTICLE_HOST = "mp.weixin.qq.com"
IMAGE_HOST_SUFFIXES = (".qpic.cn", ".qlogo.cn", ".weixin.qq.com")
_SCHEDULE_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


class StageStatus(StrEnum):
    NOT_RUN = "not_run"
    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"
    NEEDS_LOGIN = "needs_login"
    NEEDS_VERIFICATION = "needs_verification"


class DeliveryStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FrozenWindow:
    start: datetime
    end: datetime
    timezone: str

    def contains(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self.start <= timestamp < self.end


@dataclass(frozen=True)
class AccountConfig:
    name: str
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    enabled: bool = True
    account_key: str = ""
    feed_url_env: str = ""


@dataclass(frozen=True)
class Candidate:
    account: str
    title: str
    url: str
    discovered_at: datetime
    relative_time: str | None = None
    candidate_id: str | None = None


@dataclass(frozen=True)
class ImageEvidence:
    evidence_id: str
    source_url: str
    relative_path: str | None
    sha256: str | None
    status: StageStatus
    error: str | None = None


@dataclass(frozen=True)
class Article:
    article_id: str
    account: str
    title: str
    url: str
    publish_time: datetime | None
    publish_time_basis: str | None
    sanitized_html: str
    text: str
    fetch_status: StageStatus
    images: tuple[ImageEvidence, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class StageError:
    stage: str
    code: str
    message: str
    account: str | None = None
    article_id: str | None = None


@dataclass
class DigestReport:
    run_id: str
    kind: TaskKind
    target_date: str
    window: FrozenWindow
    generated_at: datetime
    source_status: StageStatus
    analysis_status: StageStatus
    fetch_status: StageStatus = StageStatus.NOT_RUN
    coverage_status: StageStatus = StageStatus.NEEDS_VERIFICATION
    publication_status: StageStatus = StageStatus.NOT_RUN
    delivery_status: DeliveryStatus = DeliveryStatus.NOT_REQUESTED
    articles: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    errors: list[StageError] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfigurationError(ValueError):
    pass


class UnsafeUrlError(ValueError):
    pass


@dataclass(frozen=True)
class TaskSettings:
    enabled: bool
    schedule: str
    analysis_enabled: bool = False
    catch_up_today: bool = False
    late_lookback_hours: int = 72


@dataclass(frozen=True)
class BrowserSettings:
    channel: str = "chrome"
    headless: bool = False


@dataclass(frozen=True)
class DeliverySettings:
    enabled: bool = False
    channel: str = "smtp"
    host: str = ""
    port: int = 465
    security: str = "ssl"
    sender_env: str = "DIGEST_MAIL_FROM"
    recipient_env: str = "DIGEST_MAIL_TO"
    username_env: str = "DIGEST_SMTP_USER"
    password_env: str = "DIGEST_SMTP_PASSWORD"


@dataclass(frozen=True)
class AppSettings:
    timezone: str
    total_timeout_seconds: int
    recruitment: TaskSettings
    practice: TaskSettings
    browser: BrowserSettings
    delivery: DeliverySettings


@dataclass(frozen=True)
class Accounts:
    recruitment: tuple[AccountConfig, ...]
    practice: tuple[AccountConfig, ...]


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as error:
        raise ConfigurationError(f"配置文件不存在：{path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"TOML 格式错误：{path}: {error}") from error


def _task_settings(data: dict, section: str) -> TaskSettings:
    raw = data.get(section)
    if not isinstance(raw, dict):
        raise ConfigurationError(f"缺少 [{section}] 配置")
    schedule = str(raw.get("schedule", ""))
    if not _SCHEDULE_PATTERN.fullmatch(schedule):
        raise ConfigurationError(f"[{section}].schedule 必须是 HH:MM")
    lookback = raw.get("late_lookback_hours", 72)
    if not isinstance(lookback, int) or not 0 <= lookback <= 168:
        raise ConfigurationError(f"[{section}].late_lookback_hours 必须是 0 到 168 的整数")
    return TaskSettings(
        enabled=bool(raw.get("enabled", True)),
        schedule=schedule,
        analysis_enabled=bool(raw.get("analysis_enabled", False)),
        catch_up_today=bool(raw.get("catch_up_today", False)),
        late_lookback_hours=lookback,
    )


def _delivery_settings(data: dict) -> DeliverySettings:
    raw = data.get("delivery", {})
    if not isinstance(raw, dict):
        raise ConfigurationError("[delivery] 必须是对象")
    channel = str(raw.get("channel", "smtp")).strip().lower()
    security = str(raw.get("security", "ssl")).strip().lower()
    port = raw.get("port", 465)
    if channel != "smtp":
        raise ConfigurationError("首版 delivery.channel 只能是 smtp")
    if security not in {"ssl", "starttls"}:
        raise ConfigurationError("delivery.security 只能是 ssl 或 starttls")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigurationError("delivery.port 必须是有效端口")
    return DeliverySettings(
        enabled=bool(raw.get("enabled", False)),
        channel=channel,
        host=str(raw.get("host", "")).strip(),
        port=port,
        security=security,
        sender_env=str(raw.get("sender_env", "DIGEST_MAIL_FROM")).strip(),
        recipient_env=str(raw.get("recipient_env", "DIGEST_MAIL_TO")).strip(),
        username_env=str(raw.get("username_env", "DIGEST_SMTP_USER")).strip(),
        password_env=str(raw.get("password_env", "DIGEST_SMTP_PASSWORD")).strip(),
    )


def load_settings(path: Path) -> AppSettings:
    data = _read_toml(path)
    timezone = str(data.get("timezone", DEFAULT_TIMEZONE)).strip()
    if timezone != DEFAULT_TIMEZONE:
        raise ConfigurationError("首版仅支持 Asia/Shanghai")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ConfigurationError(f"系统缺少时区数据：{timezone}") from error
    timeout = data.get("total_timeout_seconds", 1800)
    if not isinstance(timeout, int) or not 60 <= timeout <= 7200:
        raise ConfigurationError("total_timeout_seconds 必须是 60 到 7200 的整数")
    browser = data.get("browser", {})
    channel = str(browser.get("channel", "chrome")).strip()
    if channel not in {"chrome", "msedge"}:
        raise ConfigurationError("browser.channel 只能是 chrome 或 msedge")
    return AppSettings(
        timezone=timezone,
        total_timeout_seconds=timeout,
        recruitment=_task_settings(data, "recruitment"),
        practice=_task_settings(data, "practice"),
        browser=BrowserSettings(channel=channel, headless=bool(browser.get("headless", False))),
        delivery=_delivery_settings(data),
    )


def load_accounts(path: Path) -> Accounts:
    data = _read_toml(path)
    seen: set[tuple[str, str]] = set()

    def parse_group(group: str) -> tuple[AccountConfig, ...]:
        raw_items = data.get(group, [])
        if not isinstance(raw_items, list):
            raise ConfigurationError(f"{group} 必须是 TOML 表数组")
        result = []
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                raise ConfigurationError(f"{group}[{index}] 必须是对象")
            name = str(raw.get("name", "")).strip()
            if not name:
                raise ConfigurationError(f"{group}[{index}].name 不能为空")
            key = (group, name.casefold())
            if key in seen:
                raise ConfigurationError(f"{group} 中公众号重名：{name}")
            seen.add(key)
            include = tuple(
                str(item).strip() for item in raw.get("include", []) if str(item).strip()
            )
            exclude = tuple(
                str(item).strip() for item in raw.get("exclude", []) if str(item).strip()
            )
            if group == "practice" and not include:
                raise ConfigurationError(f"practice[{index}] 至少需要一个 include 关键词")
            account_key = str(raw.get("account_key", "")).strip()
            feed_url_env = str(raw.get("feed_url_env", "")).strip()
            if account_key and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", account_key):
                raise ConfigurationError(
                    f"{group}[{index}].account_key 只能使用 2-64 位小写字母、数字、_、-"
                )
            if feed_url_env and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", feed_url_env):
                raise ConfigurationError(f"{group}[{index}].feed_url_env 不是有效环境变量名")
            result.append(
                AccountConfig(
                    name=name,
                    include=include,
                    exclude=exclude,
                    enabled=bool(raw.get("enabled", True)),
                    account_key=account_key,
                    feed_url_env=feed_url_env,
                )
            )
        return tuple(result)

    accounts = Accounts(parse_group("recruitment"), parse_group("practice"))
    if not accounts.recruitment and not accounts.practice:
        raise ConfigurationError("至少配置一个公众号")
    return accounts


def freeze_window(
    target_date: date, schedule: str, timezone: str = DEFAULT_TIMEZONE
) -> FrozenWindow:
    hour, minute = (int(part) for part in schedule.split(":"))
    zone = ZoneInfo(timezone)
    end = datetime.combine(target_date, time(hour, minute), tzinfo=zone)
    return FrozenWindow(start=end - timedelta(seconds=86400), end=end, timezone=timezone)


def safe_article_url(value: str) -> str:
    parsed = urlparse(value.strip())
    token_path = re.fullmatch(r"/s/[A-Za-z0-9_-]+", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != ARTICLE_HOST:
        raise UnsafeUrlError("只允许 https://mp.weixin.qq.com/s?... 原文链接")
    query = parse_qs(parsed.query, keep_blank_values=False)
    canonical_query = parsed.path == "/s" and "__biz" in query
    signed_query = parsed.path == "/s" and {
        "src",
        "timestamp",
        "signature",
    }.issubset(query)
    if not token_path and not canonical_query and not signed_query:
        raise UnsafeUrlError("微信原文链接缺少可识别参数")
    return urlunparse(("https", ARTICLE_HOST, parsed.path, "", parsed.query, ""))


def article_identity(value: str) -> str:
    """Return a conservative stable key without discarding identity-bearing parameters."""
    safe_url = safe_article_url(value)
    parsed = urlparse(safe_url)
    token = re.fullmatch(r"/s/(?P<token>[A-Za-z0-9_-]+)", parsed.path)
    if token:
        material = f"token:{token.group('token')}"
    else:
        query = parse_qs(parsed.query, keep_blank_values=False)
        identity_names = ("__biz", "mid", "idx", "sn")
        identity = [(name, query[name][0]) for name in identity_names if query.get(name)]
        if {name for name, _ in identity} >= {"__biz", "mid", "idx"}:
            material = f"query:{urlencode(identity)}"
        else:
            material = f"url:{safe_url}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def is_safe_image_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        ipaddress.ip_address(parsed.hostname)
        return False
    except ValueError:
        pass
    host = parsed.hostname.lower()
    return host == "mmbiz.qpic.cn" or host.endswith(IMAGE_HOST_SUFFIXES)


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.translate(str.maketrans({"（": "(", "）": ")", "【": "[", "】": "]"}))
    return re.sub(r"[\s\u200b\ufeff]+", "", normalized)


def practice_title_matches(title: str, account: AccountConfig) -> bool:
    normalized = normalize_title(title)
    includes = [normalize_title(item) for item in account.include]
    excludes = [normalize_title(item) for item in account.exclude]
    return any(item in normalized for item in includes) and not any(
        item in normalized for item in excludes
    )


def deduplicate_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen_candidate_ids: set[str] = set()
    seen_article_ids: set[str] = set()
    result = []
    for candidate in candidates:
        candidate_id = candidate.candidate_id
        article_id = article_identity(candidate.url)
        if candidate_id and candidate_id in seen_candidate_ids:
            continue
        if article_id in seen_article_ids:
            continue
        if candidate_id:
            seen_candidate_ids.add(candidate_id)
        seen_article_ids.add(article_id)
        result.append(candidate)
    return result
