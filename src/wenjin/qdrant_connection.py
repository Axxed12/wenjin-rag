"""统一创建 Qdrant 客户端，避免意外继承宿主机代理配置。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qdrant_client import QdrantClient


def create_qdrant_client(**kwargs: Any) -> QdrantClient:
    """创建 QdrantClient；默认直连，且保留SDK支持的其他构造参数。

    httpx 默认读取环境代理。Windows 系统代理可能把 loopback Qdrant 请求
    转发到代理服务器，导致误报 401/502 或连接失败；本地服务默认不应走代理。
    需要代理的特殊调用仍可显式传 `trust_env=True`。

    QdrantClient 在函数内导入：模块被导入但未连接数据库时不加载客户端，
    同时让测试可以在运行时替换 `qdrant_client.QdrantClient`。
    """
    kwargs.setdefault("trust_env", False)
    from qdrant_client import QdrantClient

    return QdrantClient(**kwargs)
