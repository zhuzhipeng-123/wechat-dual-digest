from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.core import DigestReport, StageStatus


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"无法序列化 {type(value).__name__}")


class ReportPublisher:
    def __init__(self, template_dir: Path, output_root: Path) -> None:
        self.output_root = output_root
        self.environment = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=True,
            undefined=StrictUndefined,
        )

    def publish(
        self,
        report: DigestReport,
        preview: bool = False,
        asset_source_dir: Path | None = None,
    ) -> tuple[Path, Path]:
        if preview:
            run_dir = self.output_root / "preview" / report.run_id
            fixed_dir = run_dir
        else:
            fixed_dir = self.output_root / report.target_date
            run_dir = fixed_dir / "runs" / report.run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        if asset_source_dir is not None and asset_source_dir.is_dir():
            shutil.copytree(asset_source_dir, run_dir / "assets")
        report.publication_status = StageStatus.OK
        payload = asdict(report)
        json_name = f"{report.kind}.json"
        html_name = "招聘.html" if report.kind == "recruitment" else "练习.html"
        json_path = run_dir / json_name
        html_path = run_dir / html_name
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        template = self.environment.get_template(f"{report.kind}.html.j2")
        html_text = template.render(report=report, report_json=json_name)
        html_path.write_text(html_text, encoding="utf-8")
        manifest = {
            "run_id": report.run_id,
            "kind": report.kind,
            "html": html_name,
            "json": json_name,
            "assets": sorted(
                path.relative_to(run_dir).as_posix()
                for path in (run_dir / "assets").glob("**/*")
                if path.is_file()
            )
            if (run_dir / "assets").is_dir()
            else [],
        }
        (run_dir / ".complete.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if preview:
            return html_path, json_path
        fixed_dir.mkdir(parents=True, exist_ok=True)
        self._activate(fixed_dir, report.run_id, html_name)
        return html_path, json_path

    @staticmethod
    def _activate(fixed_dir: Path, run_id: str, html_name: str) -> Path:
        fixed_html = fixed_dir / html_name
        temporary_html = fixed_dir / f".{run_id}.{html_name}.tmp"
        target = f"runs/{run_id}/{html_name}"
        temporary_html.write_text(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0;url={target}">'
            f'<title>打开日报</title></head><body><a href="{target}">打开完整日报</a>'
            "</body></html>",
            encoding="utf-8",
        )
        os.replace(temporary_html, fixed_html)
        return fixed_html

    def activate_existing(self, target_date: str, run_id: str, kind: str) -> Path:
        fixed_dir = self.output_root / target_date
        run_dir = fixed_dir / "runs" / run_id
        manifest_path = run_dir / ".complete.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("完整版本清单不存在")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("run_id") != run_id or manifest.get("kind") != kind:
            raise ValueError("完整版本清单不匹配")
        html_name = str(manifest.get("html", ""))
        json_name = str(manifest.get("json", ""))
        if not html_name or not json_name:
            raise ValueError("完整版本清单缺少报告文件")
        if not (run_dir / html_name).is_file() or not (run_dir / json_name).is_file():
            raise FileNotFoundError("完整版本文件缺失")
        fixed_dir.mkdir(parents=True, exist_ok=True)
        return self._activate(fixed_dir, run_id, html_name)
