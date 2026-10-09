"""供 CLI 和未来 MCP 共用的 OSG 检索服务。

正式服务第一版固定使用已验证的 baseline：dense + BM25 -> RRF -> 可选本地重排。
分数仅用于排序；证据是否足够支持最终答案仍需独立检查。
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from wenjin.indexing.bm25 import bm25_query, query_terms
from wenjin.indexing.dense import dense_query
from wenjin.retrieval.fusion import rrf_fuse
from wenjin.retrieval.rerank import candidate_document, rank_candidates


@dataclass(frozen=True)
class SearchConfig:
    """单次查询预算和版本约束；限制后可安全暴露为工具参数。"""

    version: str = "3.6.5"
    candidate_k: int = 10
    top_k: int = 5
    rrf_k: int = 60
    rerank_pool: int = 30
    context_char_budget: int = 8000

    def validate(self) -> None:
        if not self.version or len(self.version) > 64:
            raise ValueError("version不能为空且不能超过64字符")
        if not 1 <= self.candidate_k <= 50:
            raise ValueError("candidate_k 必须在 1 到 50 之间")
        if not 1 <= self.top_k <= min(self.candidate_k, 20):
            raise ValueError("top_k 必须在 1 到 min(candidate_k, 20) 之间")
        if self.rrf_k < 0:
            raise ValueError("rrf_k 不能为负")
        if not self.top_k <= self.rerank_pool <= 200:
            raise ValueError("rerank_pool必须不小于top_k且不超过200")
        if not 500 <= self.context_char_budget <= 20000:
            raise ValueError("context_char_budget 必须在 500 到 20000 之间")


def load_source_urls(manifest_path: Path) -> dict[str, str]:
    """从已校验的来源清单映射原始文件到上游链接。

    两个字段和两种路径前缀都必须兼容，否则证据会丢掉 source_url：
    - OSG manifest 用 `indexable`，五库新 manifest 用 `retrieval_eligible`；
    - OSG 由 `ingest.load_documents` 读取，source_path 就是 manifest 里的相对路径；
      新语料由 `multiformat.load_retrieval_documents` 读取，source_path 额外带
      `raw_data/<raw_data_subdir>/` 前缀。
    两种键一起返回，多出的键不影响查表。
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    subdir = manifest.get("raw_data_subdir")
    urls: dict[str, str] = {}
    for entry in manifest["files"]:
        if not (entry.get("indexable") or entry.get("retrieval_eligible")):
            continue
        urls[entry["path"]] = entry["source_url"]
        if subdir:
            urls[f"raw_data/{subdir}/{entry['path']}"] = entry["source_url"]
    return urls


def select_context(results: list[dict], *, top_k: int, char_budget: int) -> list[dict]:
    """按最终名次选择完整片段，不截断正文以免来源行号失真。"""
    selected = []
    used = 0
    for final_rank, result in enumerate(results[:top_k], start=1):
        length = len(result["text"])
        if used + length > char_budget:
            continue
        copied = dict(result)
        copied["final_rank"] = final_rank
        selected.append(copied)
        used += length
    return selected


def evidence_record(result: dict, source_urls: dict[str, str], *, rank: int) -> dict:
    """把候选转成可引用证据；rank从1开始。"""
    source_path = result["source_path"]
    return {
        "rank": result.get("final_rank", rank),
        "chunk_id": result["chunk_id"],
        "version": result["version"],
        "source_path": source_path,
        "source_url": source_urls.get(source_path),
        "start_line": result["start_line"],
        "end_line": result["end_line"],
        "page_number": result.get("page_number"),
        "logical_document_id": result.get("logical_document_id"),
        # 证据必须自带来源性质：OCR识别的正文不能和原文文本长得一样。
        # 旧索引没有这两个字段时为 None，表示未知，而不是"确认是原文"。
        "parser": result.get("parser"),
        "ocr_derived": result.get("ocr_derived"),
        "text": result["text"],
        "rrf_score": result.get("rrf_score"),
        "rerank_score": result.get("rerank_score"),
        "retrieval_ranks": result.get("ranks", {}),
    }


class RetrievalService:
    """封装现有索引、Embedding和Reranker；模型在进程内复用。"""

    def __init__(
        self,
        *,
        qdrant_client: Any,
        embedder: Any,
        bm25_path: Path,
        source_urls: dict[str, str],
        collection_name: str,
        supported_version: str = "3.6.5",
        index_target_id: str | None = None,
        reranker: Any | None = None,
        source_context_cache: Any | None = None,
        dense_search: Callable[..., list] = dense_query,
        keyword_search: Callable[..., list[dict]] = bm25_query,
        search_lock: Any | None = None,
    ) -> None:
        if not bm25_path.is_file():
            raise FileNotFoundError(f"BM25 索引不存在：{bm25_path}")
        self.qdrant_client = qdrant_client
        self.embedder = embedder
        self.bm25_path = bm25_path
        self.source_urls = source_urls
        self.collection_name = collection_name
        self.supported_version = supported_version
        self.index_target_id = index_target_id
        self.reranker = reranker
        self.source_context_cache = source_context_cache
        self.dense_search = dense_search
        self.keyword_search = keyword_search
        # 锁跟随模型执行资源共享；组合多个服务/路由时，调用方可注入同一把锁。
        # 未注入时仍为独立实例创建锁，保持 CLI 等独立调用方式。
        self.search_lock = search_lock if search_lock is not None else Lock()

    def search(self, query: str, *, config: SearchConfig | None = None) -> dict:
        """返回可引用片段、检索阶段和预算；不生成答案。"""
        with self.search_lock:
            return self._search_unlocked(query, config=config)

    def _search_unlocked(self, query: str, *, config: SearchConfig | None = None) -> dict:
        """锁内执行检索；拆出此方法便于保持参数检查和数据流可读。"""
        config = config or SearchConfig()
        config.validate()
        if config.version != self.supported_version:
            raise ValueError(f"当前active索引只包含 {self.supported_version}")
        query = query.strip()
        if not 1 <= len(query) <= 2000:
            raise ValueError("query 长度必须在 1 到 2000 字符之间")

        dense_started = time.perf_counter()
        points = self.dense_search(
            self.qdrant_client,
            self.embedder,
            self.collection_name,
            query,
            top_k=config.candidate_k,
        )
        dense = [{"score": point.score, **point.payload} for point in points]
        dense_seconds = time.perf_counter() - dense_started
        terms = query_terms(query)
        bm25_started = time.perf_counter()
        keyword = (
            self.keyword_search(self.bm25_path, query, top_k=config.candidate_k)
            if terms else []
        )
        bm25_seconds = time.perf_counter() - bm25_started
        fusion_started = time.perf_counter()
        fused = rrf_fuse({"dense": dense, "bm25": keyword}, k=config.rrf_k)
        # 与跨库路由保持相同口径：每个召回通道按candidate_k取候选，RRF后最多
        # rerank_pool条进入重排，避免单库兼容入口额外截断到candidate_k。
        candidates = [item for item in fused if item["version"] == config.version][
            :config.rerank_pool
        ]
        fusion_seconds = time.perf_counter() - fusion_started
        rerank_started = time.perf_counter()
        source_context_statuses: dict[str, int] = {}
        if self.reranker is not None and candidates:
            documents = []
            for candidate in candidates:
                if self.source_context_cache is None:
                    document, status = candidate_document(candidate), "unregistered_target_profile"
                else:
                    document, status = self.source_context_cache.document_for_candidate(candidate)
                documents.append(document)
                source_context_statuses[status] = source_context_statuses.get(status, 0) + 1
            scores = self.reranker.score(query, documents)
            ranked = rank_candidates(candidates, scores)
        else:
            ranked = candidates
        rerank_seconds = time.perf_counter() - rerank_started
        selected = select_context(
            ranked,
            top_k=config.top_k,
            char_budget=config.context_char_budget,
        )
        evidence = [
            evidence_record(result, self.source_urls, rank=rank)
            for rank, result in enumerate(selected, start=1)
        ]
        return {
            "query": query,
            "version": config.version,
            "collection": self.collection_name,
            "index_target_id": self.index_target_id,
            "retrieval": {
                "candidate_k": config.candidate_k,
                "rerank_pool": config.rerank_pool,
                "dense_returned": len(dense),
                "bm25_returned": len(keyword),
                "bm25_terms": terms,
                "fused_candidates": len(candidates),
                "reranker_used": self.reranker is not None,
                "source_context_diagnostics": source_context_statuses,
                "timing_seconds": {
                    "dense": round(dense_seconds, 6),
                    "bm25": round(bm25_seconds, 6),
                    "fusion": round(fusion_seconds, 6),
                    "rerank": round(rerank_seconds, 6),
                },
            },
            "context_char_budget": config.context_char_budget,
            "context_chars_used": sum(len(item["text"]) for item in evidence),
            "evidence": evidence,
            "evidence_status": "unverified",
            "evidence_guidance": (
                "The retrieval service found ranked passages but did not verify that they jointly "
                "support every part of the query. The client must inspect evidence before answering."
            ),
            "answer_status": "not_generated",
            "score_note": "RRF/reranker scores are ranking signals, not answer confidence",
        }

    def close(self) -> None:
        """释放服务持有的客户端和模型；可用于CLI退出或服务关闭。"""
        with self.search_lock:
            self.qdrant_client.close()
            if self.reranker is not None:
                self.reranker.close()
            bindings = getattr(self, "model_bindings", None)
            if bindings is not None:
                bindings.close()
