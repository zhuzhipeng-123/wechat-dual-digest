from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from bs4 import BeautifulSoup

from app.core import DeliverySettings, DeliveryStatus, TaskKind

MAX_EMAIL_BYTES = 20 * 1024 * 1024


class DeliveryConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    message: str


def _required_environment(
    settings: DeliverySettings, environment: dict[str, str]
) -> dict[str, str]:
    names = {
        "sender": settings.sender_env,
        "recipient": settings.recipient_env,
        "username": settings.username_env,
        "password": settings.password_env,
    }
    missing = [variable for variable in names.values() if not environment.get(variable, "").strip()]
    if not settings.host:
        raise DeliveryConfigurationError("delivery.host 未配置")
    if missing:
        raise DeliveryConfigurationError(f"邮件环境变量未配置：{', '.join(missing)}")
    return {key: environment[name].strip() for key, name in names.items()}


def build_message(
    *,
    run_id: str,
    kind: TaskKind,
    target_date: str,
    status: str,
    html_path: Path,
    sender: str,
    recipient: str,
) -> EmailMessage:
    html_text = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_text, "html.parser")
    inline_assets: list[tuple[str, Path]] = []
    asset_cids: dict[Path, str] = {}
    for image in soup.find_all("img"):
        source = str(image.get("src", ""))
        source_path = Path(source)
        if not source or source_path.is_absolute() or ".." in source_path.parts:
            continue
        asset_path = (html_path.parent / source_path).resolve()
        if not asset_path.is_file() or html_path.parent.resolve() not in asset_path.parents:
            continue
        cid = asset_cids.get(asset_path)
        if cid is None:
            cid = f"{run_id}-asset-{len(asset_cids) + 1}"
            asset_cids[asset_path] = cid
            inline_assets.append((cid, asset_path))
        image["src"] = f"cid:{cid}"

    label = "招聘日报" if kind == "recruitment" else "练习日报"
    message = EmailMessage()
    message["Subject"] = f"{label} · {target_date} · {status}"
    message["From"] = sender
    message["To"] = recipient
    message["Message-ID"] = f"<{run_id}@wechat-dual-digest.local>"
    message.set_content(
        f"{label} {target_date} 已生成。此邮件包含可移植 HTML 正文；原文链接请在 HTML 版本中打开。"
    )
    message.add_alternative(str(soup), subtype="html")
    html_part = message.get_payload()[-1]
    for cid, asset_path in inline_assets:
        mime, _ = mimetypes.guess_type(asset_path.name)
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        html_part.add_related(
            asset_path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            cid=f"<{cid}>",
            filename=asset_path.name,
        )
    if len(message.as_bytes()) > MAX_EMAIL_BYTES:
        raise DeliveryConfigurationError("邮件内容超过 20 MiB；未静默丢弃正文或图片")
    return message


class SMTPDelivery:
    def __init__(
        self,
        settings: DeliverySettings,
        *,
        environment: dict[str, str] | None = None,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    ) -> None:
        self.settings = settings
        self.environment = environment if environment is not None else os.environ
        self.smtp_factory = smtp_factory

    def send(
        self,
        *,
        run_id: str,
        kind: TaskKind,
        target_date: str,
        report_status: str,
        html_path: Path,
    ) -> DeliveryResult:
        if not self.settings.enabled:
            return DeliveryResult(DeliveryStatus.NOT_REQUESTED, "邮件交付未启用")
        try:
            values = _required_environment(self.settings, self.environment)
            message = build_message(
                run_id=run_id,
                kind=kind,
                target_date=target_date,
                status=report_status,
                html_path=html_path,
                sender=values["sender"],
                recipient=values["recipient"],
            )
        except (DeliveryConfigurationError, OSError, ValueError) as error:
            return DeliveryResult(DeliveryStatus.FAILED, str(error))

        try:
            client = self._connect()
            try:
                if self.settings.security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                client.login(values["username"], values["password"])
                client.send_message(message)
            finally:
                with suppress(smtplib.SMTPException):
                    client.quit()
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused) as error:
            return DeliveryResult(
                DeliveryStatus.FAILED, f"邮件服务明确拒绝：{type(error).__name__}"
            )
        except (TimeoutError, smtplib.SMTPServerDisconnected) as error:
            return DeliveryResult(
                DeliveryStatus.UNKNOWN,
                f"发送连接中断，服务端是否接收无法确定：{type(error).__name__}",
            )
        except (smtplib.SMTPException, OSError) as error:
            return DeliveryResult(DeliveryStatus.FAILED, f"邮件发送失败：{type(error).__name__}")
        return DeliveryResult(DeliveryStatus.SENT, "邮件服务已接收；实际收件仍需客户端核对")

    def _connect(self) -> smtplib.SMTP:
        if self.smtp_factory is not None:
            return self.smtp_factory(self.settings.host, self.settings.port, timeout=30)
        if self.settings.security == "ssl":
            return smtplib.SMTP_SSL(
                self.settings.host,
                self.settings.port,
                timeout=30,
                context=ssl.create_default_context(),
            )
        return smtplib.SMTP(self.settings.host, self.settings.port, timeout=30)
