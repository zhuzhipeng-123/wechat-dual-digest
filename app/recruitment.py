from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from app.agnes import AgnesClient

CITY_TIERS = {
    "北京": "T1",
    "天津": "T1",
    "上海": "T2",
    "南京": "T2",
    "杭州": "T2",
    "苏州": "T2",
    "郑州": "T3",
    "西安": "T3",
}
NON_TARGET_PATTERN = re.compile(r"销售|市场|行政|前台|客服|商务拓展|渠道")
TARGET_PATTERN = re.compile(r"算法|统计|数据|数字化|人工智能|AI|机器学习|大模型", re.IGNORECASE)

EXTRACTION_SCHEMA = {
    "type": "object",
    "required": ["aid", "is_recruitment", "is_pure_internship", "companies"],
    "additionalProperties": False,
    "properties": {
        "aid": {"type": "string", "minLength": 1},
        "is_recruitment": {"type": "boolean"},
        "is_pure_internship": {"type": "boolean"},
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "company",
                    "employer_type",
                    "company_scale",
                    "classification_evidence_ids",
                    "jobs",
                    "source_title",
                    "source_account",
                    "source_publish_time",
                    "source_link",
                ],
                "properties": {
                    "company": {"type": "string", "minLength": 1},
                    "employer_type": {"enum": ["央国企", "互联网企业", "其他企业", "性质待确认"]},
                    "company_scale": {"enum": ["大型", "中型", "小型", "未确认"]},
                    "classification_evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "jobs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "locations", "evidence_ids", "employment_type"],
                            "properties": {
                                "name": {"type": "string", "minLength": 1},
                                "locations": {"type": "array", "items": {"type": "string"}},
                                "evidence_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string"},
                                },
                                "employment_type": {"enum": ["校招全职", "实习", "社招", "待核实"]},
                            },
                        },
                    },
                    "source_title": {"type": "string"},
                    "source_account": {"type": "string"},
                    "source_publish_time": {"type": "string"},
                    "source_link": {"type": "string"},
                },
            },
        },
    },
}


class ModelJSONError(ValueError):
    pass


class ExtractionValidationError(ValueError):
    pass


class OCRError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRBlock:
    evidence_id: str
    text: str
    confidence: float


@dataclass(frozen=True)
class EvidenceBlock:
    evidence_id: str
    text: str
    kind: str


def parse_model_json(text: str) -> object:
    if not isinstance(text, str):
        raise ModelJSONError("模型没有返回文本")
    value = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ModelJSONError("模型输出不是一个完整合法的 JSON 值") from error


def _employer_tier(employer_type: str, company_scale: str) -> str:
    if employer_type == "互联网企业" or (employer_type == "央国企" and company_scale == "大型"):
        return "E0"
    if employer_type in {"央国企", "其他企业"}:
        return "E1"
    return "E?"


def _city_tier(locations: list[str]) -> str:
    tiers = {CITY_TIERS.get(location.strip(), "其他") for location in locations if location.strip()}
    return next((tier for tier in ("T1", "T2", "T3", "其他") if tier in tiers), "未确认")


def rate_company(employer_type: str, company_scale: str, jobs: list[dict]) -> str:
    e_tier = _employer_tier(employer_type, company_scale)
    c_tier = _city_tier([location for job in jobs for location in job.get("locations", [])])
    matrix = {
        ("E0", "T1"): "P0",
        ("E0", "T2"): "P1",
        ("E0", "T3"): "P2",
        ("E1", "T1"): "P1",
        ("E1", "T2"): "P2",
        ("E?", "T1"): "P2",
    }
    rating = matrix.get((e_tier, c_tier), "P3")
    known_names = [str(job.get("name", "")).strip() for job in jobs if job.get("name")]
    if known_names and all(
        NON_TARGET_PATTERN.search(name) and not TARGET_PATTERN.search(name) for name in known_names
    ):
        return "P3"
    return rating


def validate_extraction(payload: Any, source: dict[str, str], evidence: dict[str, str]) -> dict:
    errors = sorted(
        Draft202012Validator(EXTRACTION_SCHEMA).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if errors:
        raise ExtractionValidationError(f"Schema 校验失败：{errors[0].message}")
    assert isinstance(payload, dict)
    if payload["aid"] != source["aid"]:
        raise ExtractionValidationError("来源字段不匹配：aid")
    source_fields = ("source_title", "source_account", "source_publish_time", "source_link")
    for company in payload["companies"]:
        for key in source_fields:
            if company[key] != source[key]:
                raise ExtractionValidationError(f"来源字段不匹配：{key}")
        references = company["classification_evidence_ids"] + [
            item for job in company["jobs"] for item in job["evidence_ids"]
        ]
        missing = [item for item in references if item not in evidence]
        if missing:
            raise ExtractionValidationError(f"引用了不存在的证据：{missing[0]}")
        company["jobs"] = [job for job in company["jobs"] if job["employment_type"] == "校招全职"]
    if payload["is_pure_internship"] and payload["companies"]:
        raise ExtractionValidationError("纯实习文章不得输出企业卡")
    if not payload["is_recruitment"] and payload["companies"]:
        raise ExtractionValidationError("非招聘文章不得输出企业卡")
    duplicate = [
        name
        for name, count in Counter(c["company"] for c in payload["companies"]).items()
        if count > 1
    ]
    if duplicate:
        raise ExtractionValidationError(f"同篇企业重复：{duplicate[0]}")
    return payload


class LocalOCR:
    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as error:
            raise OCRError("未安装 rapidocr-onnxruntime") from error
        self.engine = RapidOCR()

    def read(self, image_path: Path) -> tuple[OCRBlock, ...]:
        if not image_path.is_file():
            raise OCRError(f"图片不存在：{image_path.name}")
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()[:16]
        try:
            result, _ = self.engine(str(image_path))
        except Exception as error:
            raise OCRError(f"OCR 失败：{type(error).__name__}") from error
        if not result:
            return ()
        return tuple(
            OCRBlock(f"ocr-{digest}-{index}", str(item[1]).strip(), float(item[2]))
            for index, item in enumerate(result, start=1)
            if str(item[1]).strip()
        )


SYSTEM_PROMPT = """你只做招聘事实抽取，输出一个 JSON 对象，不写评级、推荐、自然段或 HTML。
只使用输入证据，不用常识补企业性质、规模、岗位或地点。只保留校招全职岗位。
岗位必须分别绑定自己的地点和 evidence_ids。来源字段必须逐字复制输入。"""


def extract_recruitment(
    client: AgnesClient,
    source: dict[str, str],
    blocks: list[EvidenceBlock],
    max_tokens: int = 1800,
) -> dict:
    evidence = {block.evidence_id: block.text for block in blocks}
    prompt = json.dumps(
        {
            "source": source,
            "evidence": [block.__dict__ for block in blocks],
            "schema": EXTRACTION_SCHEMA,
        },
        ensure_ascii=False,
    )
    reply = client.complete_json(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    validated = validate_extraction(parse_model_json(reply.content), source, evidence)
    for company in validated["companies"]:
        company["rating"] = rate_company(
            company["employer_type"], company["company_scale"], company["jobs"]
        )
    validated.update(model=reply.model, usage=reply.usage)
    return validated
