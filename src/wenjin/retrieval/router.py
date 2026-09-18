"""跨语料库检索路由：显式 library + version 优先，未指定时才受预算约束扇出。

与单库 RetrievalService 的区别：
- 每个库的候选必须先通过独立的三资产校验，否则构造路由时就失败（fail closed）；
- 只有调用方省略 library 时才查询多个库，并用 max_libraries 限制扇出；
- query 只编码一次，复用到所有 collection；单张本地 GPU 上检索仍按库顺序执行，
  避免并发进入同一 Embedding/Reranker 模型造成显存峰值。
检索分数只用于排序，不作为答案置信度。
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from wenjin.index_registry import IndexTarget, resolve_project_path, validate_target
from wenjin.indexing.bm25 import bm25_query, query_terms
from wenjin.indexing.dense import dense_query_vector
from wenjin.retrieval.fusion import rrf_fuse
from wenjin.retrieval.rerank import candidate_document, rank_candidates
from wenjin.retrieval.service import evidence_record, load_source_urls, select_context
from wenjin.serving import download_path_for


def normalize_library(name: str) -> str:
    """把调用方写法归一到路由ID：大小写、空格和下划线不敏感。"""
    return re.sub(r"[\s_]+", "-", name.strip().lower())


@dataclass(frozen=True)
class RoutedIndex:
    """一个已通过校验、可参与路由的索引。"""

    target: IndexTarget
    collection: str
    bm25_path: Path
    source_urls: dict[str, str]

    @property
    def library(self) -> str:
        return normalize_library(self.target.library)

    @property
    def version(self) -> str:
        return self.target.version

    @property
    def corpus_identity(self) -> str:
        """语料身份取自IndexTarget，而不是在这里重新推导目录名。"""
        return self.target.corpus_identity()


@dataclass(frozen=True)
class RouteConfig:
    """跨库查询预算；每个字段都同时约束单库候选数和总开销。"""

    candidate_k: int = 10  # 每个库、每路（dense/BM25）返回的候选上限
    top_k: int = 5
    rrf_k: int = 60
    max_libraries: int = 5
    rerank_pool: int = 30  # 送入重排的全局候选上限
    context_char_budget: int = 8000

    def validate(self) -> None:
        if not 1 <= self.candidate_k <= 50:
            raise ValueError("candidate_k 必须在 1 到 50 之间")
        if not 1 <= self.top_k <= min(self.candidate_k, 20):
            raise ValueError("top_k 必须在 1 到 min(candidate_k, 20) 之间")
        if self.rrf_k < 0:
            raise ValueError("rrf_k 不能为负")
        if not 1 <= self.max_libraries <= 20:
            raise ValueError("max_libraries 必须在 1 到 20 之间")
        if not self.top_k <= self.rerank_pool <= 200:
            raise ValueError("rerank_pool 必须不小于 top_k 且不超过 200")
        if not 500 <= self.context_char_budget <= 20000:
            raise ValueError("context_char_budget 必须在 500 到 20000 之间")


class MultiCorpusRouter:
    """按 library/version 选择索引，并在需要时做受预算约束的跨库检索。"""

    def __init__(
        self,
        *,
        qdrant_client: Any,
        embedder: Any,
        indices: list[RoutedIndex],
        reranker: Any | None = None,
        dense_search: Callable[..., list] = dense_query_vector,
        keyword_search: Callable[..., list[dict]] = bm25_query,
    ) -> None:
        if not indices:
            raise ValueError("路由至少需要一个已校验索引")
        self.qdrant_client = qdrant_client
        self.embedder = embedder
        self.indices = indices
        self.reranker = reranker
        self.dense_search = dense_search
        self.keyword_search = keyword_search
        # 与单库服务一致：本地模型串行执行，避免并发显存峰值。
        self._search_lock = Lock()

    @classmethod
    def from_route_file(
        cls,
        route_path: Path,
        *,
        root: Path,
        qdrant_client: Any,
        embedder: Any,
        reranker: Any | None = None,
    ) -> "MultiCorpusRouter":
        """读取候选清单并逐个校验；任一候选不通过就不建立路由。"""
        route = json.loads(route_path.read_text(encoding="utf-8-sig"))
        if route.get("schema_version") != 1 or not route.get("candidates"):
            raise ValueError("路由清单schema或候选列表无效")
        indices = []
        for item in route["candidates"]:
            target = IndexTarget.from_dict(item)
            validate_target(target, root=root, qdrant_client=qdrant_client)
            indices.append(
                RoutedIndex(
                    target=target,
                    collection=target.dense_collection,
                    bm25_path=resolve_project_path(root, target.bm25_path),
                    source_urls=load_source_urls(
                        resolve_project_path(root, target.manifest_path)
                    ),
                )
            )
        return cls(
            qdrant_client=qdrant_client,
            embedder=embedder,
            indices=indices,
            reranker=reranker,
        )

    def catalog(self) -> list[dict]:
        """列出路由中每个库的版本、chunk数和target，供调用方选择。"""
        return [
            {
                "library": index.library,
                "version": index.version,
                "target_id": index.target.target_id,
                "chunks": index.target.chunk_count,
            }
            for index in self.indices
        ]

    def resolve(self, library: str | None, version: str | None = None) -> list[RoutedIndex]:
        """显式路由：指定library时必须唯一匹配；省略library时返回全部（受预算限制）。"""
        if library is None:
            if version is not None:
                matched = [index for index in self.indices if index.version == version]
                if not matched:
                    raise ValueError(f"没有版本为 {version} 的路由索引")
                return matched
            return list(self.indices)
        wanted = normalize_library(library)
        matched = [index for index in self.indices if index.library == wanted]
        if not matched:
            available = ", ".join(sorted({index.library for index in self.indices}))
            raise ValueError(f"没有library为 {library} 的索引；可用：{available}")
        if version is not None:
            matched = [index for index in matched if index.version == version]
            if not matched:
                versions = ", ".join(sorted({index.version for index in self.indices
                                             if index.library == wanted}))
                raise ValueError(f"library {library} 没有版本 {version}；可用：{versions}")
        return matched

    def retrieve(
        self,
        query: str,
        *,
        library: str | None = None,
        version: str | None = None,
        config: RouteConfig | None = None,
    ) -> dict:
        """执行检索并返回各阶段候选；不生成答案，也不判定证据是否充分。"""
        with self._search_lock:
            return self._retrieve_unlocked(query, library=library, version=version, config=config)

    def _retrieve_unlocked(
        self,
        query: str,
        *,
        library: str | None,
        version: str | None,
        config: RouteConfig | None,
    ) -> dict:
        config = config or RouteConfig()
        config.validate()
        query = query.strip()
        if not 1 <= len(query) <= 2000:
            raise ValueError("query 长度必须在 1 到 2000 字符之间")
        matched = self.resolve(library, version)
        # 只有省略library时max_libraries才会真正裁掉库；被裁掉的库必须显式报告。
        selected = matched[: config.max_libraries]
        skipped = [index.library for index in matched[config.max_libraries :]]

        started = time.perf_counter()
        embedding_started = time.perf_counter()
        vector = self.embedder.get_query_embedding(query)
        embedding_seconds = time.perf_counter() - embedding_started
        terms = query_terms(query)

        dense: list[dict] = []
        keyword: list[dict] = []
        per_library = []
        for index in selected:
            dense_started = time.perf_counter()
            points = self.dense_search(
                self.qdrant_client, vector, index.collection, top_k=config.candidate_k
            )
            dense_seconds = time.perf_counter() - dense_started
            library_dense = [
                {"library": index.library, "score": point.score, **point.payload}
                for point in points
            ]
            bm25_started = time.perf_counter()
            library_keyword = (
                [
                    {"library": index.library, **item}
                    for item in self.keyword_search(
                        index.bm25_path, query, top_k=config.candidate_k
                    )
                ]
                if terms
                else []
            )
            bm25_seconds = time.perf_counter() - bm25_started
            dense.extend(library_dense)
            keyword.extend(library_keyword)
            per_library.append(
                {
                    "library": index.library,
                    "version": index.version,
                    "target_id": index.target.target_id,
                    "dense_returned": len(library_dense),
                    "bm25_returned": len(library_keyword),
                    "timing_seconds": {
                        "dense": round(dense_seconds, 6),
                        "bm25": round(bm25_seconds, 6),
                    },
                }
            )

        fusion_started = time.perf_counter()
        # 每个库的dense和BM25各自作为一条排名列表参与RRF：RRF只用名次，
        # 因此不需要让不同语料库的原始分数可比。
        result_sets: dict[str, list[dict]] = {}
        for index in selected:
            for route, candidates in (("dense", dense), ("bm25", keyword)):
                library_candidates = [
                    item for item in candidates if item["library"] == index.library
                ]
                if library_candidates:
                    result_sets[f"{index.library}:{route}"] = library_candidates
        fused = rrf_fuse(result_sets, k=config.rrf_k)[: config.rerank_pool]
        fusion_seconds = time.perf_counter() - fusion_started
        rerank_started = time.perf_counter()
        if self.reranker is not None and fused:
            scores = self.reranker.score(
                query, [candidate_document(candidate) for candidate in fused]
            )
            ranked = rank_candidates(fused, scores)
        else:
            ranked = fused
        rerank_seconds = time.perf_counter() - rerank_started
        return {
            "query": query,
            "requested": {"library": library, "version": version},
            "routed_libraries": [
                {"library": index.library, "version": index.version,
                 "target_id": index.target.target_id}
                for index in selected
            ],
            "skipped_libraries": skipped,
            "dense_candidates": dense,
            "bm25_candidates": keyword,
            "fused_candidates": fused,
            "ranked_candidates": ranked,
            "per_library": per_library,
            "bm25_terms": terms,
            "reranker_used": self.reranker is not None,
            "timing_seconds": {
                "query_embedding": round(embedding_seconds, 6),
                "fusion": round(fusion_seconds, 6),
                "rerank": round(rerank_seconds, 6),
                "total": round(time.perf_counter() - started, 6),
            },
        }

    def search(
        self,
        query: str,
        *,
        library: str | None = None,
        version: str | None = None,
        config: RouteConfig | None = None,
    ) -> dict:
        """返回可引用证据和路由诊断；不生成答案，也不声明证据充分。"""
        config = config or RouteConfig()
        retrieval = self.retrieve(query, library=library, version=version, config=config)
        indices = {index.library: index for index in self.indices}
        selected = select_context(
            retrieval["ranked_candidates"],
            top_k=config.top_k,
            char_budget=config.context_char_budget,
        )
        evidence = []
        for rank, result in enumerate(selected, start=1):
            index = indices[result["library"]]
            record = evidence_record(result, index.source_urls, rank=rank)
            record["library"] = result["library"]
            record["index_target_id"] = index.target.target_id
            # 取件路径只做拼接；真正下载时由端点按访问策略鉴权。
            record["download_path"] = download_path_for(
                index.corpus_identity, result["source_path"]
            )
            evidence.append(record)
        return {
            "query": retrieval["query"],
            "requested": retrieval["requested"],
            "routed_libraries": retrieval["routed_libraries"],
            "skipped_libraries": retrieval["skipped_libraries"],
            "retrieval": {
                "dense_returned": len(retrieval["dense_candidates"]),
                "bm25_returned": len(retrieval["bm25_candidates"]),
                "bm25_terms": retrieval["bm25_terms"],
                "fused_candidates": len(retrieval["fused_candidates"]),
                "reranker_used": retrieval["reranker_used"],
                "per_library": retrieval["per_library"],
                "timing_seconds": retrieval["timing_seconds"],
            },
            "context_char_budget": config.context_char_budget,
            "context_chars_used": sum(len(item["text"]) for item in evidence),
            "evidence": evidence,
            "evidence_status": "unverified",
            "evidence_guidance": (
                "The router returned ranked passages from verified indexes but did not verify "
                "that they jointly support every part of the query. The client must inspect "
                "evidence before answering."
            ),
            "answer_status": "not_generated",
            "score_note": (
                "RRF/reranker scores are ranking signals, not answer confidence; "
                "BM25 scores are not comparable across libraries"
            ),
        }

    def close(self) -> None:
        """释放客户端和模型；调用方负责不再使用本路由。"""
        self.qdrant_client.close()
        if self.reranker is not None:
            self.reranker.close()
