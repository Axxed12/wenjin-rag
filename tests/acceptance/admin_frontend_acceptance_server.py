"""仅供管理前端验收的隔离 HTTP 服务。

服务使用调用者显式指定的 root、进程内 Qdrant 和固定标记向量器；不会读取项目
Settings、真实 embedding 模型、项目 Qdrant 或任何现存 corpus/index。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import uvicorn
from qdrant_client import QdrantClient

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tests.support.admin_shared_fixture import build_admin_shared_fixture  # noqa: E402
from wenjin.file_management import FileManagementService  # noqa: E402
from wenjin.file_management.api import create_app, validate_listen_security  # noqa: E402


class MarkerEmbedder:
    """小型确定性测试向量器；结果只用于验证链路，不代表检索质量。"""

    dimension = 64

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in text.lower().split():
            index = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16) % self.dimension
            vector[index] += 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    def get_text_embedding_batch(self, texts, show_progress=False):
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


def create_isolated_app(
    root: Path, *, token: str, include_shared_fixture: bool = False
):
    """Build isolated app resources; the optional shared fixture stays under root."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    qdrant = QdrantClient(":memory:")
    embedder = MarkerEmbedder()
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=embedder,
        embedding_model="isolated-marker-test-only",
    )
    try:
        fixture = (
            build_admin_shared_fixture(root, qdrant, embedder)
            if include_shared_fixture
            else None
        )
        app = create_app(root, admin_token=token, manager=manager)
    except Exception:
        manager.close()
        qdrant.close()
        raise
    return app, manager, qdrant, fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="仅供本轮验收使用的隔离临时项目根目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--token", default="admin-preview-isolated-token")
    parser.add_argument(
        "--include-shared-fixture",
        action="store_true",
        help="在隔离root内构建合成共享语料/正式候选双索引，用于shared查询验收",
    )
    args = parser.parse_args()
    validate_listen_security(args.host, args.token)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("该验收server仅允许loopback监听")
    if not 1 <= args.port <= 65535:
        raise ValueError("port必须在1到65535之间")

    root = args.root.resolve()
    app, manager, qdrant, fixture = create_isolated_app(
        root,
        token=args.token,
        include_shared_fixture=args.include_shared_fixture,
    )
    print(f"isolated_root={root}", flush=True)
    print(f"preview_url=http://{args.host}:{args.port}/admin/", flush=True)
    if fixture is not None:
        print(f"shared_fixture={fixture}", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
    finally:
        manager.close()
        qdrant.close()


if __name__ == "__main__":
    main()
