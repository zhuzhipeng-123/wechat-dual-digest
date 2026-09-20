from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core import PROJECT_ROOT, AccountConfig, Article, Candidate, StageStatus
from app.pipeline import build_report
from app.reporting import ReportPublisher
from app.source import WeReadDiscoveryProbe


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
                datetime(2026, 9, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
                candidate_id=f"demo-{self.kind}",
            )
        ]


class DemoFetcher:
    def fetch(self, url: str, expected_account: str = "") -> Article:
        practice = "练习" in expected_account
        return Article(
            "demo-practice" if practice else "demo-recruitment",
            expected_account,
            "每日一题｜合成示例" if practice else "2027 届校园招聘｜合成示例",
            url,
            datetime(2026, 9, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
            "synthetic fixture",
            "<p>这是离线合成内容，不代表真实微信文章。</p>",
            "这是离线合成内容，不代表真实微信文章。",
            StageStatus.OK,
        )


def generate_samples(project_root: Path) -> list[Path]:
    generated_at = datetime(2026, 9, 20, 13, tzinfo=ZoneInfo("Asia/Shanghai"))
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


def menu() -> int:
    options = {
        "1": "检查/登录微信读书",
        "2": "生成并打开离线样例",
        "3": "打开最新正式报告目录",
        "4": "启动调度器（公众号发现仍未可靠验证）",
        "0": "退出",
    }
    while True:
        print("\n微信公众号双日报 MVP")
        for key, label in options.items():
            print(f"  {key}. {label}")
        choice = input("请选择：").strip()
        if choice == "0":
            return 0
        if choice == "1":
            run_login_probe()
        elif choice == "2":
            paths = generate_samples(PROJECT_ROOT)
            html_paths = [path for path in paths if path.suffix == ".html"]
            if html_paths:
                webbrowser.open(html_paths[0].as_uri())
            print("已生成离线合成样例；这不是微信或 Agnes 实测。")
        elif choice == "3":
            _open_path(PROJECT_ROOT / "output")
        elif choice == "4":
            print("调度器未启动：公众号发现会漏文或触发验证，不能承诺准时完整送达。")
        else:
            print("无效选项。")


def main() -> int:
    if len(sys.argv) == 1:
        return menu()
    command = sys.argv[1]
    if command == "probe-login":
        return run_login_probe()
    if command == "samples":
        for path in generate_samples(PROJECT_ROOT):
            print(path)
        return 0
    if command == "test":
        return subprocess.call([sys.executable, "-m", "pytest"])
    print(f"未知命令：{command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
