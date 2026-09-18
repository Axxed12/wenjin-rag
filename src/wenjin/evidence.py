"""用OpenAI兼容模型判断检索证据是否完整支持问题。"""

import json
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from wenjin.retrieval.query_rewrite import QueryRewriteSettings

VALID_STATUSES = {"supported", "partially_supported", "insufficient"}


@dataclass(frozen=True)
class EvidenceAssessment:
    status: str
    citations: tuple[int, ...]
    supported_claims: tuple[str, ...]
    missing_aspects: tuple[str, ...]
    reason: str


def build_evidence_messages(query: str, evidence: list[dict]) -> list[dict[str, str]]:
    """把证据作为编号JSON数据传入，要求模型只判断支持性。"""
    compact = [
        {
            "rank": item["rank"],
            "version": item["version"],
            "source_path": item["source_path"],
            "start_line": item["start_line"],
            "end_line": item["end_line"],
            "text": item["text"],
        }
        for item in evidence
    ]
    system = """你是严格的检索证据审核器，不使用外部知识，不补全代码含义，不生成最终答案。
判断给定证据是否明确支持用户问题的每一个要求：
- supported：所有要求都有明确证据；
- partially_supported：至少一个要求有证据，但仍缺少其他必要部分；
- insufficient：没有直接证据、版本不匹配，或只有相似但不同的API/概念。
不能因资料中没出现某内容就断言它在完整项目中不存在。类名、方法名、版本和返回类型必须精确。
citations只能填写实际支持判断的证据rank。只输出JSON：
{"status":"supported|partially_supported|insufficient","citations":[1],
"supported_claims":["..."],"missing_aspects":["..."],"reason":"..."}"""
    payload = json.dumps({"query": query, "evidence": compact}, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": payload}]


def parse_evidence_assessment(content: str, *, available_ranks: set[int]) -> EvidenceAssessment:
    """严格校验模型JSON及引用编号，拒绝不存在的citation。"""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("证据审核模型没有返回JSON对象")
    data = json.loads(content[start : end + 1])
    status = data.get("status")
    citations = data.get("citations")
    supported_claims = data.get("supported_claims")
    missing_aspects = data.get("missing_aspects")
    reason = data.get("reason")
    if status not in VALID_STATUSES:
        raise ValueError("证据状态无效")
    if not isinstance(citations, list) or not all(isinstance(rank, int) for rank in citations):
        raise ValueError("citations必须是整数数组")
    if not set(citations) <= available_ranks:
        raise ValueError("citations包含不存在的证据编号")
    if not isinstance(supported_claims, list) or not all(
        isinstance(item, str) for item in supported_claims
    ):
        raise ValueError("supported_claims必须是字符串数组")
    if not isinstance(missing_aspects, list) or not all(
        isinstance(item, str) for item in missing_aspects
    ):
        raise ValueError("missing_aspects必须是字符串数组")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason必须是非空字符串")
    if status == "supported" and missing_aspects:
        raise ValueError("supported状态不能同时声明缺失方面")
    return EvidenceAssessment(
        status=status,
        citations=tuple(citations),
        supported_claims=tuple(item.strip() for item in supported_claims if item.strip()),
        missing_aspects=tuple(item.strip() for item in missing_aspects if item.strip()),
        reason=reason.strip(),
    )


class OpenAIEvidenceJudge:
    """复用query改写的OpenAI兼容连接配置。"""

    def __init__(
        self,
        settings: QueryRewriteSettings | None = None,
        *,
        client: Any | None = None,
        model: str | None = None,
    ) -> None:
        if client is None:
            settings = settings or QueryRewriteSettings()
            client = OpenAI(
                base_url=settings.query_rewrite_base_url,
                api_key=settings.query_rewrite_api_key.get_secret_value(),
                timeout=settings.query_rewrite_timeout_seconds,
            )
            model = settings.query_rewrite_model
        if not model:
            raise ValueError("必须提供证据审核模型名")
        self.client = client
        self.model = model

    def assess(self, query: str, evidence: list[dict]) -> EvidenceAssessment:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=build_evidence_messages(query, evidence),
            temperature=0,
            max_tokens=800,
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise ValueError("证据审核模型返回空内容")
        return parse_evidence_assessment(
            content,
            available_ranks={item["rank"] for item in evidence},
        )
