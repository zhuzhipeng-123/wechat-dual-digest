from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.core import (
    PROJECT_ROOT,
    AccountConfig,
    Article,
    Candidate,
    DeliverySettings,
    DeliveryStatus,
    StageStatus,
    TaskSettings,
    load_accounts,
    load_settings,
)
from app.delivery import SMTPDelivery
from app.pipeline import build_report, execute_claim, execute_preview
from app.reporting import ReportPublisher
from app.scheduler import DailyScheduler, RunLedger, ScheduledTask
from app.source import WeReadDiscoveryProbe

ZONE = ZoneInfo("Asia/Shanghai")


class DemoDiscoverer:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def discover(self, account: AccountConfig) -> list[Candidate]:
        title = "每日一题｜合成示例" if self.kind == "practice" else "2027 届校园招聘｜合成示例"
        return [
            Candidate(
                account.name,
                title,
                "https://mp.weixin.qq.com/s?__biz=synthetic",
                datetime(2026, 9, 20, 12, tzinfo=ZONE),
                candidate_id=f"demo-{self.kind}",
            )
        ]


class DemoFetcher:
    def fetch(self, url: str, expected_account: str = "", asset_dir: Path | None = None) -> Article:
        practice = "练习" in expected_account
        return Article(
            "demo-practice" if practice else "demo-recruitment",
            expected_account,
            "每日一题｜合成示例" if practice else "2027 届校园招聘｜合成示例",
            url,
            datetime(2026, 9, 20, 12, tzinfo=ZONE),
            "synthetic fixture",
            "<p>这是离线合成内容，不代表真实微信文章。</p>",
            "这是离线合成内容，不代表真实微信文章。",
            StageStatus.OK,
        )


def generate_samples(project_root: Path) -> list[Path]:
    generated_at = datetime(2026, 9, 20, 13, tzinfo=ZONE)
    publisher = ReportPublisher(project_root / "templates", project_root / "samples")
    paths = []
    for kind, account in (
        ("practice", AccountConfig("示例练习号", ("每日一题",), ("课程",))),
        ("recruitment", AccountConfig("示例招聘号")),
    ):
        report = build_report(
            kind=kind,
            target_date=date(2026, 9, 20),
            schedule="21:00",
            accounts=(account,),
            discoverer=DemoDiscoverer(kind),
            fetcher=DemoFetcher(),
            generated_at=generated_at,
            late_lookback_hours=0,
        )
        paths.extend(publisher.publish(report, preview=True))
    return paths


def _open_path(path: Path) -> None:
    if not path.exists():
        print(f"文件不存在：{path}")
        return
    os.startfile(path)  # type: ignore[attr-defined]


def run_login_probe() -> int:
    result = WeReadDiscoveryProbe(PROJECT_ROOT / "data" / "browser-profile").run()
    print(result)
    if result.authenticated and result.public_account_capability:
        return 0
    return 2


def _paths(project_root: Path) -> tuple[Path, Path]:
    return project_root / "config" / "settings.toml", project_root / "config" / "accounts.toml"


def check_configuration(
    project_root: Path = PROJECT_ROOT, environment: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    environment = environment if environment is not None else os.environ
    settings_path, accounts_path = _paths(project_root)
    settings = load_settings(settings_path)
    accounts = load_accounts(accounts_path)
    errors: list[str] = []
    notices: list[str] = []
    for kind, task_settings, items in (
        ("招聘", settings.recruitment, accounts.recruitment),
        ("练习", settings.practice, accounts.practice),
    ):
        if not task_settings.enabled:
            notices.append(f"{kind}日报：已禁用")
            continue
        if not any(account.enabled for account in items):
            errors.append(f"{kind}日报没有启用的账号")
        for account in items:
            if not account.enabled:
                notices.append(f"{kind}账号 {account.name}：已禁用")
                continue
            if not account.account_key:
                errors.append(f"{kind}账号 {account.name}：缺少 account_key")
            if not account.feed_url_env:
                errors.append(f"{kind}账号 {account.name}：缺少 feed_url_env")
                continue
            value = environment.get(account.feed_url_env, "").strip()
            if not value:
                errors.append(f"{kind}账号 {account.name}：环境变量 {account.feed_url_env} 未配置")
            elif urlparse(value).scheme != "https" or not urlparse(value).hostname:
                errors.append(f"{kind}账号 {account.name}：订阅地址必须使用 HTTPS")
            else:
                notices.append(f"{kind}账号 {account.name}：来源地址已配置（内容已隐藏）")
    if settings.delivery.enabled:
        if not settings.delivery.host:
            errors.append("邮件已启用，但 delivery.host 未配置")
        for variable in (
            settings.delivery.sender_env,
            settings.delivery.recipient_env,
            settings.delivery.username_env,
            settings.delivery.password_env,
        ):
            if not environment.get(variable, "").strip():
                errors.append(f"邮件已启用，但环境变量 {variable} 未配置")
    else:
        notices.append("邮件交付未启用；正式运行只生成本地报告")
    notices.append("来源覆盖仍需用目标公众号的真实多篇样本单独验收")
    return errors, notices


def run_check(project_root: Path = PROJECT_ROOT) -> int:
    try:
        errors, notices = check_configuration(project_root)
    except Exception as error:
        print(f"配置读取失败：{error}")
        return 2
    for notice in notices:
        print(f"[信息] {notice}")
    for error in errors:
        print(f"[缺项] {error}")
    return 2 if errors else 0


def run_preview(kind: str, project_root: Path = PROJECT_ROOT) -> int:
    settings_path, accounts_path = _paths(project_root)
    settings = load_settings(settings_path)
    accounts = load_accounts(accounts_path)
    generated_at = datetime.now(ZONE)
    kinds = ("recruitment", "practice") if kind == "both" else (kind,)
    exit_code = 0
    for task_kind in kinds:
        task_settings = getattr(settings, task_kind)
        task_accounts = getattr(accounts, task_kind)
        try:
            html_path, _ = execute_preview(
                kind=task_kind,
                target_date=generated_at.date(),
                schedule=task_settings.schedule,
                accounts=task_accounts,
                project_root=project_root,
                generated_at=generated_at,
                late_lookback_hours=task_settings.late_lookback_hours,
            )
            print(f"预览已生成：{html_path}")
        except Exception as error:
            exit_code = 2
            print(f"{task_kind} 预览失败：{type(error).__name__}: {error}")
    return exit_code


def _delivery_callback(settings, run_id: str):
    sender = SMTPDelivery(settings)

    def deliver(html_path, report):
        result = sender.send(
            run_id=run_id,
            kind=report.kind,
            target_date=report.target_date,
            report_status=report.source_status.value,
            html_path=html_path,
        )
        return result.status, result.message

    return deliver


def run_schedule(project_root: Path = PROJECT_ROOT) -> int:
    if run_check(project_root):
        print("调度器未启动：请先补齐以上正式来源或邮件配置。")
        return 2
    ledger = RunLedger(project_root / "data" / "runs.sqlite3")

    def runner(claim) -> None:
        snapshot = json.loads(claim.config_json)
        task_settings = TaskSettings(**snapshot["task"])
        task_accounts = tuple(
            AccountConfig(
                name=item["name"],
                include=tuple(item["include"]),
                exclude=tuple(item["exclude"]),
                enabled=item["enabled"],
                account_key=item["account_key"],
                feed_url_env=item["feed_url_env"],
            )
            for item in snapshot["accounts"]
        )
        delivery_settings = DeliverySettings(**snapshot["delivery"])
        execute_claim(
            claim=claim,
            accounts=task_accounts,
            task_settings=task_settings,
            ledger=ledger,
            project_root=project_root,
            total_timeout_seconds=snapshot["total_timeout_seconds"],
            delivery_enabled=delivery_settings.enabled,
            deliver=_delivery_callback(delivery_settings, claim.run_id),
        )

    scheduler = DailyScheduler(ledger, runner, max_workers=2)
    interrupted = ledger.list_interrupted()
    if interrupted:
        print(f"发现 {len(interrupted)} 个疑似中断运行；不会自动抢占或重发，请人工核对。")
    print("调度器已启动。仅在程序和电脑保持运行时生效；按 Ctrl+C 停止。")
    try:
        while True:
            settings_path, accounts_path = _paths(project_root)
            settings = load_settings(settings_path)
            accounts = load_accounts(accounts_path)
            now = datetime.now(ZONE)
            tasks = []
            for kind in ("recruitment", "practice"):
                task_settings = getattr(settings, kind)
                if not task_settings.enabled:
                    continue
                effective = ledger.effective_schedule(kind, task_settings.schedule, now.date())
                tasks.append(
                    ScheduledTask(
                        kind,
                        effective,
                        {
                            "task": asdict(task_settings),
                            "accounts": [asdict(item) for item in getattr(accounts, kind)],
                            "delivery": asdict(settings.delivery),
                            "total_timeout_seconds": settings.total_timeout_seconds,
                        },
                        task_settings.catch_up_today,
                    )
                )
            claims = scheduler.tick(now, tuple(tasks))
            for claim in claims:
                print(f"已领取运行：{claim.run_id}")
            time.sleep(20)
    except KeyboardInterrupt:
        print("正在等待已开始的任务安全结束……")
    finally:
        scheduler.close(wait=True)
    return 0


def retry_send(run_id: str, *, force: bool = False, project_root: Path = PROJECT_ROOT) -> int:
    settings = load_settings(_paths(project_root)[0])
    if not settings.delivery.enabled:
        print("邮件交付未启用。")
        return 2
    ledger = RunLedger(project_root / "data" / "runs.sqlite3")
    row = ledger.get(run_id)
    if row is None or not row.get("report_html"):
        print("未找到已完成报告；不会重新抓取文章。")
        return 2
    html_path = Path(row["report_html"])
    if not html_path.is_file() or not (html_path.parent / ".complete.json").is_file():
        print("报告版本不完整；拒绝发送。")
        return 2
    if not ledger.begin_delivery(run_id, force=force):
        print(f"当前发送状态为 {row['delivery_status']}，未执行重发。")
        return 2
    result = SMTPDelivery(settings.delivery).send(
        run_id=run_id,
        kind=row["kind"],
        target_date=row["target_date"],
        report_status=row["status"],
        html_path=html_path,
    )
    ledger.finish_delivery(run_id, result.status, datetime.now(ZONE), result.message)
    print(result.message)
    return 0 if result.status == DeliveryStatus.SENT else 2


def recover_report(run_id: str, *, confirm_stopped: bool, project_root: Path = PROJECT_ROOT) -> int:
    if not confirm_stopped:
        print("必须先确认旧进程已经停止，再添加 --confirm-stopped；不会自动抢占运行。")
        return 2
    ledger = RunLedger(project_root / "data" / "runs.sqlite3")
    row = ledger.get(run_id)
    if row is None:
        print("未找到该运行。")
        return 2
    run_dir = project_root / "output" / row["target_date"] / "runs" / run_id
    manifest_path = run_dir / ".complete.json"
    if not manifest_path.is_file():
        print("没有完整版本清单，拒绝恢复。")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_id or manifest.get("kind") != row["kind"]:
        print("完成清单与运行记录不匹配，拒绝恢复。")
        return 2
    html_path = run_dir / manifest["html"]
    json_path = run_dir / manifest["json"]
    if not html_path.is_file() or not json_path.is_file():
        print("完成清单引用的报告文件缺失，拒绝恢复。")
        return 2
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if payload.get("run_id") != run_id or payload.get("kind") != row["kind"]:
        print("报告数据与运行记录不匹配，拒绝恢复。")
        return 2
    ReportPublisher(project_root / "templates", project_root / "output").activate_existing(
        row["target_date"], run_id, row["kind"]
    )
    status = DeliveryStatus(payload.get("delivery_status", DeliveryStatus.NOT_REQUESTED))
    ledger.recover_completed_report(run_id, html_path, json_path, status, datetime.now(ZONE))
    for article in payload.get("articles", []):
        if not article.get("account_key") or not article.get("article_id"):
            continue
        ledger.mark_collected(
            row["kind"],
            article["account_key"],
            article["article_id"],
            article["url"],
            str(article["publish_time"]),
            run_id,
            datetime.now(ZONE),
        )
    print("已从完整版本清单恢复运行记录；没有重新抓取或发送。")
    return 0


def menu() -> int:
    options = {
        "1": "检查正式配置",
        "2": "生成并打开离线样例",
        "3": "按真实来源生成隔离预览",
        "4": "启动正式调度器",
        "5": "打开报告目录",
        "6": "诊断微信读书页面（非正式来源）",
        "0": "退出",
    }
    while True:
        print("\n微信公众号双日报")
        for key, label in options.items():
            print(f"  {key}. {label}")
        choice = input("请选择：").strip()
        if choice == "0":
            return 0
        if choice == "1":
            run_check()
        elif choice == "2":
            paths = generate_samples(PROJECT_ROOT)
            html_paths = [path for path in paths if path.suffix == ".html"]
            if html_paths:
                webbrowser.open(html_paths[0].as_uri())
            print("已生成离线合成样例；这不是微信、真实订阅或邮件实测。")
        elif choice == "3":
            run_preview("both")
        elif choice == "4":
            return run_schedule()
        elif choice == "5":
            _open_path(PROJECT_ROOT / "output")
        elif choice == "6":
            run_login_probe()
        else:
            print("无效选项。")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="微信公众号双日报")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check", help="检查正式来源和邮件配置")
    preview = subparsers.add_parser("preview", help="按真实来源生成隔离预览")
    preview.add_argument("--kind", choices=("practice", "recruitment", "both"), default="both")
    subparsers.add_parser("schedule", help="启动前台调度器")
    retry = subparsers.add_parser("retry-send", help="只重发已有完整报告")
    retry.add_argument("run_id")
    retry.add_argument("--force", action="store_true", help="显式重发 sent/unknown，可能重复")
    recover = subparsers.add_parser("recover-report", help="旧进程停止后恢复完整报告记录")
    recover.add_argument("run_id")
    recover.add_argument("--confirm-stopped", action="store_true")
    subparsers.add_parser("probe-login", help="诊断微信读书页面，不作为正式来源")
    subparsers.add_parser("samples", help="生成离线合成样例")
    subparsers.add_parser("test", help="运行离线测试")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command is None:
        return menu()
    if args.command == "check":
        return run_check()
    if args.command == "preview":
        return run_preview(args.kind)
    if args.command == "schedule":
        return run_schedule()
    if args.command == "retry-send":
        return retry_send(args.run_id, force=args.force)
    if args.command == "recover-report":
        return recover_report(args.run_id, confirm_stopped=args.confirm_stopped)
    if args.command == "probe-login":
        return run_login_probe()
    if args.command == "samples":
        for path in generate_samples(PROJECT_ROOT):
            print(path)
        return 0
    if args.command == "test":
        return subprocess.call([sys.executable, "-m", "pytest"])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
