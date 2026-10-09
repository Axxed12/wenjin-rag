"""管理/MCP进程内按需加载、由唯一生命周期owner释放的重排器。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Any


def model_tree_fingerprint(path: Path) -> dict[str, Any]:
    """记录模型目录完整内容身份，供检索owner和评测绑定共用。"""
    path = Path(path).resolve()
    if not path.is_dir():
        raise ValueError("reranker模型目录不可用")
    entries = []
    for item in sorted(child for child in path.rglob("*") if child.is_file()):
        resolved = item.resolve()
        if not resolved.is_relative_to(path):
            raise ValueError("reranker模型目录包含越界文件链接")
        digest = hashlib.sha256()
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        entries.append({
            "relative_path": item.relative_to(path).as_posix(),
            "size": item.stat().st_size,
            "sha256": digest.hexdigest(),
        })
    if not entries:
        raise ValueError("reranker模型目录为空")
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "file_count": len(entries),
        "files": entries,
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


class LazyQwen3Reranker:
    """首次score时构造模型；外部检索必须先持有共享模型执行锁。

    内部锁只防止重复构造。它从不获取调用方的search_lock，避免锁序反转。
    """

    def __init__(
        self,
        model_path: Path,
        *,
        model_id: str,
        device: str,
        max_length: int = 1024,
        batch_size: int = 2,
        factory=None,
    ) -> None:
        self.model_path = Path(model_path).resolve()
        self.model_id = str(model_id)
        self.device = str(device)
        self.max_length = int(max_length)
        self.batch_size = int(batch_size)
        self._factory = factory
        self._init_lock = Lock()
        self._instance: Any | None = None
        self._asset_fingerprint: dict[str, Any] | None = None
        self._expected_asset_sha256: str | None = None
        self._closed = False

    @property
    def loaded(self) -> bool:
        return self._instance is not None

    @property
    def loaded_asset_fingerprint(self) -> dict[str, Any] | None:
        return self._asset_fingerprint

    def bind_asset_fingerprint(self, expected_sha256: str) -> None:
        """评测固定输入绑定到共享owner；拒绝已加载旧模型或冲突绑定。"""
        expected = str(expected_sha256)
        with self._init_lock:
            if self._asset_fingerprint is not None:
                if self._asset_fingerprint.get("sha256") != expected:
                    raise RuntimeError("已加载reranker资产与固定评测输入不一致")
                return
            if self._expected_asset_sha256 not in {None, expected}:
                raise RuntimeError("共享reranker已绑定到不同的模型资产")
            self._expected_asset_sha256 = expected

    def _load(self):
        if self._closed:
            raise RuntimeError("reranker owner已关闭")
        if self._instance is None:
            with self._init_lock:
                if self._closed:
                    raise RuntimeError("reranker owner已关闭")
                if self._instance is None:
                    before = model_tree_fingerprint(self.model_path)
                    if (self._expected_asset_sha256 is not None
                            and before["sha256"] != self._expected_asset_sha256):
                        raise RuntimeError("reranker磁盘资产与固定运行身份不一致")
                    factory = self._factory
                    if factory is None:
                        from wenjin.retrieval.rerank import Qwen3Reranker

                        factory = Qwen3Reranker
                    instance = factory(
                        self.model_path,
                        device=self.device,
                        max_length=self.max_length,
                        batch_size=self.batch_size,
                    )
                    try:
                        after = model_tree_fingerprint(self.model_path)
                        if after != before:
                            raise RuntimeError("reranker加载期间模型资产发生变化")
                    except BaseException:
                        try:
                            instance.close()
                        except Exception:
                            pass
                        raise
                    self._asset_fingerprint = after
                    self._instance = instance
        return self._instance

    def score(self, query: str, documents: list[str]) -> list[float]:
        # 调用方的search_lock保护构造期间及实际模型执行；本类不再套外层模型锁。
        return self._load().score(query, documents)

    def close(self) -> None:
        """仅由注册owner在停掉请求/任务后调用，重复调用安全。"""
        with self._init_lock:
            if self._closed:
                return
            self._closed = True
            if self._instance is not None:
                self._instance.close()
                self._instance = None


def build_shared_reranker(settings: Any, *, root: Path) -> LazyQwen3Reranker | None:
    """按Settings创建一个进程内owner；模型只在首次检索评分时加载。"""
    model_path_value = getattr(settings, "reranker_model_path", None)
    if not getattr(settings, "search_reranker_enabled", model_path_value is not None):
        return None
    if model_path_value is None:
        raise RuntimeError("共享检索启用了reranker，但未配置模型路径")
    import torch

    resolver = getattr(settings, "resolved_model_path", None)
    model_path = (
        resolver("reranker_model_path", root=root)
        if callable(resolver)
        else Path(model_path_value)
    )
    if not Path(model_path).is_absolute():
        model_path = (Path(root).resolve() / model_path).resolve()
    model_id = getattr(
        settings, "resolved_reranker_model_id",
        getattr(model_path_value, "name", str(model_path_value)),
    )
    return LazyQwen3Reranker(
        model_path,
        model_id=model_id,
        device="cuda" if torch.cuda.is_available() else "cpu",
        max_length=getattr(settings, "reranker_max_length", 1024),
        batch_size=getattr(settings, "reranker_batch_size", 2),
    )
