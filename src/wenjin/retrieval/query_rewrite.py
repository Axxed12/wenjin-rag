"""通过 OpenAI 兼容接口做 query 英译与子问题拆解。

模块只改写检索请求，不生成答案。生产内网部署时可把 base_url 指向 LiteLLM；
使用公网 API 做实验时，调用方必须确认 query 不含内部敏感信息。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class QueryRewriteSettings(BaseSettings):
    """从 .env 读取改写模型配置；SecretStr 可避免密钥被意外打印。"""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )
    query_rewrite_base_url: str
    query_rewrite_api_key: SecretStr
    query_rewrite_model: str
    query_rewrite_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class QueryRewrite:
    """一次改写的结构化结果。"""

    original_query: str
    english_query: str
    subqueries: tuple[str, ...]

    def retrieval_queries(self, *, include_subqueries: bool = True) -> list[str]:
        """返回去重后的检索 query；第一项始终保留用户原问题。"""
        candidates = [self.original_query, self.english_query]
        if include_subqueries:
            candidates.extend(self.subqueries)
        unique = []
        seen = set()
        for candidate in candidates:
            normalized = candidate.strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                unique.append(normalized)
                seen.add(key)
        return unique


def build_rewrite_messages(
    query: str,
    *,
    corpus_hint: str,
    max_subqueries: int = 4,
) -> list[dict[str, str]]:
    """构造稳定提示词；用户 query 作为 JSON 数据传入，不拼进系统指令。"""
    if not 1 <= max_subqueries <= 6:
        raise ValueError("max_subqueries 必须在 1 到 6 之间")
    system = f"""你是检索 query 改写器，不回答问题。
目标语料：{corpus_hint}
任务：
1. 将原问题忠实改写为一条适合该语料的英文检索 query。
2. 如果问题需要多处证据，拆成 1 到 {max_subqueries} 条原子检索 query。
3. 保留版本号、类名、函数名、错误码等已有约束。
4. 不得声称答案，不得把不确定事实写成已知事实；子问题可以描述要核实的关系。
只输出 JSON 对象，格式为：
{{"english_query":"...","subqueries":["...", "..."]}}"""
    user = json.dumps({"query": query}, ensure_ascii=False)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _extract_json_object(content: str) -> dict[str, Any]:
    """兼容模型偶尔返回的 Markdown 围栏，但仍严格要求其中是 JSON 对象。"""
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("改写模型没有返回 JSON 对象")
    data = json.loads(content[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("改写结果必须是 JSON 对象")
    return data


def parse_rewrite_response(
    original_query: str,
    content: str,
    *,
    max_subqueries: int = 4,
) -> QueryRewrite:
    """把模型文本转成强约束对象，防止空 query 或无限拆解进入检索器。"""
    data = _extract_json_object(content)
    english_query = data.get("english_query")
    subqueries = data.get("subqueries")
    if not isinstance(english_query, str) or not english_query.strip():
        raise ValueError("english_query 必须是非空字符串")
    if not isinstance(subqueries, list) or not all(isinstance(item, str) for item in subqueries):
        raise ValueError("subqueries 必须是字符串数组")
    cleaned = tuple(item.strip() for item in subqueries if item.strip())
    if not cleaned:
        raise ValueError("subqueries 至少包含一条有效 query")
    return QueryRewrite(
        original_query=original_query.strip(),
        english_query=english_query.strip(),
        subqueries=cleaned[:max_subqueries],
    )


class OpenAIQueryRewriter:
    """最薄的模型适配层，便于用 LiteLLM 或其他 OpenAI 兼容服务替换后端。"""

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
            raise ValueError("必须提供 query rewrite 模型名")
        self.client = client
        self.model = model

    def rewrite(
        self,
        query: str,
        *,
        corpus_hint: str,
        max_subqueries: int = 4,
    ) -> QueryRewrite:
        """调用 Chat Completions；解析失败直接报错，由上层决定是否退回原 query。"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=build_rewrite_messages(
                query,
                corpus_hint=corpus_hint,
                max_subqueries=max_subqueries,
            ),
            temperature=0,
            max_tokens=600,
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise ValueError("改写模型返回了空内容")
        return parse_rewrite_response(
            query,
            content,
            max_subqueries=max_subqueries,
        )


def rewrite_or_fallback(
    rewriter: OpenAIQueryRewriter,
    query: str,
    *,
    corpus_hint: str,
    max_subqueries: int = 4,
) -> tuple[QueryRewrite, str | None]:
    """模型调用失败时只使用原 query，并返回可记录但不含密钥的异常类型。"""
    try:
        result = rewriter.rewrite(
            query,
            corpus_hint=corpus_hint,
            max_subqueries=max_subqueries,
        )
        return result, None
    except Exception as exc:  # 网络、限流、模型输出格式错误都由上层统一降级。
        fallback = QueryRewrite(
            original_query=query,
            english_query=query,
            subqueries=(query,),
        )
        return fallback, type(exc).__name__
