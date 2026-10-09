"""管理共享检索与MCP共用的检索预算和安全可观察配置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wenjin.retrieval.router import RouteConfig
from wenjin.retrieval.service import SearchConfig


@dataclass(frozen=True)
class SearchPolicy:
    """统一入口的基础候选深度、重排预算及模型声明。"""

    candidate_k: int = 10
    rerank_pool: int = 30
    rrf_k: int = 60
    max_libraries: int = 5
    context_char_budget: int = 8000
    reranker_enabled: bool = True
    reranker_model_id: str = "Qwen3-Reranker-0.6B"
    reranker_max_length: int = 1024
    reranker_batch_size: int = 2

    @classmethod
    def from_settings(cls, settings: Any) -> "SearchPolicy":
        model_path = getattr(settings, "reranker_model_path", None)
        resolved_model_id = getattr(settings, "resolved_reranker_model_id", None)
        if not resolved_model_id and model_path is not None:
            resolved_model_id = getattr(model_path, "name", str(model_path))
        policy = cls(
            candidate_k=int(getattr(settings, "search_candidate_k", 10)),
            rerank_pool=int(getattr(settings, "search_rerank_pool", 30)),
            rrf_k=int(getattr(settings, "search_rrf_k", 60)),
            max_libraries=int(getattr(settings, "search_max_libraries", 5)),
            context_char_budget=int(getattr(settings, "search_context_char_budget", 8000)),
            reranker_enabled=bool(getattr(
                settings, "search_reranker_enabled", model_path is not None
            )),
            reranker_model_id=str(resolved_model_id or "Qwen3-Reranker-0.6B"),
            reranker_max_length=int(getattr(settings, "reranker_max_length", 1024)),
            reranker_batch_size=int(getattr(settings, "reranker_batch_size", 2)),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if not 1 <= self.candidate_k <= 50:
            raise ValueError("search_candidate_k必须在1到50之间")
        if not 1 <= self.rerank_pool <= 200:
            raise ValueError("search_rerank_pool必须在1到200之间")
        if self.rrf_k < 0 or not 1 <= self.max_libraries <= 20:
            raise ValueError("共享检索RRF或跨库预算无效")
        if not 500 <= self.context_char_budget <= 20000:
            raise ValueError("search_context_char_budget必须在500到20000之间")
        if not self.reranker_model_id or not 1 <= self.reranker_max_length <= 8192:
            raise ValueError("reranker模型身份或max_length无效")
        if not 1 <= self.reranker_batch_size <= 128:
            raise ValueError("reranker_batch_size必须在1到128之间")

    def effective_candidate_k(self, top_k: int) -> int:
        """top_k较大时自动扩充召回槽，两个入口使用同一规则。"""
        if not 1 <= int(top_k) <= 20:
            raise ValueError("top_k必须在1到20之间")
        return max(self.candidate_k, int(top_k))

    def effective_rerank_pool(self, top_k: int) -> int:
        return max(self.rerank_pool, int(top_k))

    def route_config(self, top_k: int, *, context_char_budget: int | None = None) -> RouteConfig:
        return RouteConfig(
            candidate_k=self.effective_candidate_k(top_k),
            top_k=int(top_k),
            rrf_k=self.rrf_k,
            max_libraries=self.max_libraries,
            rerank_pool=self.effective_rerank_pool(top_k),
            context_char_budget=(self.context_char_budget if context_char_budget is None
                                 else int(context_char_budget)),
        )

    def single_config(
        self, version: str, top_k: int, *, context_char_budget: int | None = None
    ) -> SearchConfig:
        return SearchConfig(
            version=version,
            candidate_k=self.effective_candidate_k(top_k),
            top_k=int(top_k),
            rrf_k=self.rrf_k,
            rerank_pool=self.effective_rerank_pool(top_k),
            context_char_budget=(self.context_char_budget if context_char_budget is None
                                 else int(context_char_budget)),
        )

    def public_config(
        self, top_k: int, *, context_char_budget: int | None = None
    ) -> dict[str, Any]:
        """返回可解释的生效配置，不含密钥、模型绝对路径或连接信息。"""
        model_id = self.reranker_model_id.replace("\\", "/").rsplit("/", 1)[-1]
        return {
            "candidate_k": self.effective_candidate_k(top_k),
            "base_candidate_k": self.candidate_k,
            "rerank_pool": self.effective_rerank_pool(top_k),
            "top_k": int(top_k),
            "context_char_budget": (self.context_char_budget if context_char_budget is None
                                     else int(context_char_budget)),
            "reranker_enabled": self.reranker_enabled,
            "reranker_model_id": model_id,
            "reranker_max_length": self.reranker_max_length,
            "reranker_batch_size": self.reranker_batch_size,
            "rrf_k": self.rrf_k,
            "max_libraries": self.max_libraries,
        }
