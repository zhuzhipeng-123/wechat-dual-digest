from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.core import DigestReport


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

    def publish(self, report: DigestReport, preview: bool = False) -> tuple[Path, Path]:
        if preview:
            run_dir = self.output_root / "preview" / report.run_id
            fixed_dir = run_dir
        else:
            fixed_dir = self.output_root / report.target_date
            run_dir = fixed_dir / "runs" / report.run_id
        run_dir.mkdir(parents=True, exist_ok=False)
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
        if preview:
            return html_path, json_path
        fixed_dir.mkdir(parents=True, exist_ok=True)
        fixed_json = fixed_dir / json_name
        fixed_html = fixed_dir / html_name
        temporary_json = fixed_dir / f".{report.run_id}.{json_name}.tmp"
        temporary_html = fixed_dir / f".{report.run_id}.{html_name}.tmp"
        temporary_json.write_bytes(json_path.read_bytes())
        temporary_html.write_bytes(html_path.read_bytes())
        os.replace(temporary_json, fixed_json)
        os.replace(temporary_html, fixed_html)
        return fixed_html, fixed_json
