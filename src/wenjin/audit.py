"""MCP 工具调用的最小结构化审计；默认不保存 Query 正文。"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO
from uuid import uuid4


def digest(value: str) -> str:
    """相同输入得相同 SHA256，方便关联调用而不保存正文。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SearchAudit:
    """单进程 JSONL 审计；默认stdout，文件模式自行轮转。"""

    def __init__(
        self,
        path: Path | None = None,
        *,
        stream: TextIO | None = None,
        include_query: bool = False,
        enabled: bool = True,
    ) -> None:
        if path is not None and stream is not None:
            raise ValueError("path和stream只能设置一个")
        self.include_query = include_query
        self.enabled = enabled
        self.logger = logging.getLogger(f"wenjin.audit.{uuid4().hex}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not enabled:
            self.handler: logging.Handler = logging.NullHandler()
        elif path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handler = RotatingFileHandler(
                path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        else:
            self.handler = logging.StreamHandler(stream or sys.stdout)
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(self.handler)

    @classmethod
    def from_environment(cls, root: Path) -> "SearchAudit":
        """stdout适合Docker；file用于无日志平台环境；off只保留响应request_id。"""
        sink = os.getenv("WENJIN_AUDIT_SINK", "stdout").strip().lower()
        include_query = os.getenv("WENJIN_AUDIT_INCLUDE_QUERY") == "1"
        if sink == "stdout":
            return cls(include_query=include_query)
        if sink == "file":
            configured = os.getenv("WENJIN_AUDIT_PATH")
            path = Path(configured) if configured else root / "artifacts/audit/search_docs.jsonl"
            return cls(path, include_query=include_query)
        if sink == "off":
            return cls(include_query=include_query, enabled=False)
        raise ValueError("WENJIN_AUDIT_SINK必须为stdout、file或off")

    def record(
        self,
        *,
        query: str,
        version: str,
        top_k: int,
        context_char_budget: int,
        session_id: str | None,
        elapsed_seconds: float,
        client_id: str | None = None,
        result: dict | None = None,
        error: Exception | None = None,
    ) -> str:
        """返回请求 ID；日志不含 API key、原文片段或原始会话 ID。"""
        request_id = uuid4().hex
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "event": "mcp_tool_audit",
            "service": "wenjin-rag",
            "request_id": request_id,
            "tool": "search_docs",
            "session_id_sha256": digest(session_id) if session_id else None,
            "client_id_sha256": digest(client_id) if client_id else None,
            "query_sha256": digest(query),
            "query_length": len(query),
            "version": version,
            "top_k": top_k,
            "context_char_budget": context_char_budget,
            "status": "error" if error else "success",
            "error_type": type(error).__name__ if error else None,
            "elapsed_seconds": round(elapsed_seconds, 6),
            "returned_chunk_ids": [item.get("chunk_id") for item in result.get("evidence", [])] if result else [],
            "source_paths": [item.get("source_path") for item in result.get("evidence", [])] if result else [],
            "retrieval_timing_seconds": result.get("retrieval", {}).get("timing_seconds") if result else None,
        }
        if self.include_query:
            row["query"] = query
        if self.enabled:
            self.logger.info(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return request_id

    def record_download(
        self,
        *,
        corpus_id: str,
        relative_path: str,
        client_id: str | None,
        outcome: str,
        elapsed_seconds: float,
        bytes_served: int | None = None,
        detail: str | None = None,
    ) -> str:
        """记录一次原文取件。

        `outcome` 取值：served / denied / not_found / hash_mismatch / too_large / error。
        只记语料内相对路径——它是定位文件必需的，也不含绝对路径；正文、密钥和绝对路径都不记。
        """
        request_id = uuid4().hex
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "event": "mcp_download_audit",
            "service": "wenjin-rag",
            "request_id": request_id,
            "corpus_id": corpus_id,
            "relative_path": relative_path,
            "client_id_sha256": digest(client_id) if client_id else None,
            "outcome": outcome,
            "bytes_served": bytes_served,
            "elapsed_seconds": round(elapsed_seconds, 6),
            "detail": detail,
        }
        if self.enabled:
            self.logger.info(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return request_id

    def record_dropped_evidence_details(
        self, *, query: str, dropped: list[dict], client_id: str | None
    ) -> None:
        """记录被丢弃证据的完整明细——**只进服务端审计，不返回客户端**。

        客户端只收到数量和原因码；语料名、路径和chunk ID 留在这里供排查。
        query只记哈希，明细里不含正文。
        """
        if not self.enabled or not dropped:
            return
        row = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "event": "mcp_evidence_dropped",
            "service": "wenjin-rag",
            "query_sha256": digest(query),
            "client_id_sha256": digest(client_id) if client_id else None,
            "count": len(dropped),
            "details": dropped,
        }
        self.logger.info(json.dumps(row, ensure_ascii=False, separators=(",", ":")))

    def close(self) -> None:
        self.handler.close()
        self.logger.removeHandler(self.handler)
