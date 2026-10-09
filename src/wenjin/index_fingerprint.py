"""索引资产的只读内容指纹。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_cosine_vector(value):
    """按既有合同规范化COSINE方向，忽略float32末位抖动。"""
    if isinstance(value, dict):
        return {
            key: _canonical_cosine_vector(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
            norm = math.sqrt(math.fsum(float(item) * float(item) for item in value))
            if norm == 0:
                return [0.0 for _item in value]
            return [round(float(item) / norm, 7) for item in value]
        return [_canonical_cosine_vector(item) for item in value]
    return value


def qdrant_fingerprint(client, collection: str) -> str:
    """对collection的全部ID、payload和向量形成稳定指纹。"""
    info = client.get_collection(collection)
    vector_config = info.config.params.vectors
    dump = getattr(vector_config, "model_dump", None)
    vector_descriptor = dump(mode="json") if dump else str(vector_config)
    distance = getattr(vector_config, "distance", None)
    is_cosine = str(getattr(distance, "value", distance)).lower() == "cosine"
    points_fingerprints: list[tuple[str, str]] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for point in points:
            point_id = str(point.id)
            vector = _canonical_cosine_vector(point.vector) if is_cosine else point.vector
            item = {"id": point_id, "payload": point.payload or {}, "vector": vector}
            points_fingerprints.append(
                (point_id, hashlib.sha256(canonical_json(item)).hexdigest())
            )
        if offset is None:
            break
    digest = hashlib.sha256()
    # 距离度量和向量空间属于检索语义；相同点数据换成另一度量也必须算不同快照。
    digest.update(canonical_json({"vectors_config": vector_descriptor}))
    digest.update(b"\n")
    for point_id, fingerprint in sorted(points_fingerprints):
        digest.update(point_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(fingerprint.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
