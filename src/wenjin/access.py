"""语料访问策略：默认拒绝，缺策略就不给看。

**作用边界要说清楚**：本项目能做的只是"执行策略"，不能"产生策略"。系统知道的是"哪个客户端"
（Bearer 密钥映射出的 client_id），不知道"哪个人"——身份源和授权源都在内网 IAM 里，不在本项目
范围内。所以这里的静态 ACL 是策略执行点，内网部署时应替换成从 IAM 读取的实现；
`AccessPolicy.allows()` 这个窄接口不变，调用方（检索、取件、列表）不需要跟着改。

不依赖 MCP、Qdrant 或检索模块，只做"这个 client 能不能读这个语料"的判断。

策略文件是**部署配置，不进Git**：client_id 与部署环境绑定，内部语料的 corpus_id 也会
泄露内部目录命名。
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 1
WILDCARD = "*"


class AccessDenied(PermissionError):
    """调用方没有被授予该语料的访问权。"""


@dataclass(frozen=True)
class AccessPolicy:
    corpus_id: str
    visibility: str
    allowed_clients: tuple[str, ...]
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict, *, expected_corpus_id: str | None = None) -> "AccessPolicy":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("访问策略schema_version无效")
        corpus_id = str(data.get("corpus_id") or "").strip()
        if not corpus_id:
            raise ValueError("访问策略缺少corpus_id")
        if expected_corpus_id is not None and corpus_id != expected_corpus_id:
            raise ValueError(f"访问策略的corpus_id与文件名不一致：{corpus_id}")
        visibility = data.get("visibility", "internal")
        if visibility not in {"public", "internal"}:
            raise ValueError(f"visibility只能是public或internal：{visibility}")
        clients = data.get("allowed_clients")
        if not isinstance(clients, list) or not all(isinstance(item, str) for item in clients):
            raise ValueError("allowed_clients必须是字符串列表")
        return cls(corpus_id, visibility, tuple(item.strip() for item in clients if item.strip()),
                   str(data.get("note", "")))

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "corpus_id": self.corpus_id,
            "visibility": self.visibility,
            "allowed_clients": list(self.allowed_clients),
            "note": self.note,
        }

    def allows(self, client_id: str | None) -> bool:
        """判断一个客户端能否读取本语料。

        `"*"` 表示"公开语料，明确放行"——包括没有配置 Bearer、因此没有客户端身份的部署。
        这不是漏洞而是显式选择：没配置鉴权时本来就没有身份可用，策略能做的最多是"公开/不公开"。
        内部语料一律列出具体 client_id，此时未认证(None)和未列出的客户端都会被拒绝。
        """
        if WILDCARD in self.allowed_clients:
            return True
        if not client_id:
            return False
        return client_id in self.allowed_clients


def policy_path(directory: Path, corpus_id: str) -> Path:
    return directory / f"{corpus_id}.json"


def load_policy(directory: Path, corpus_id: str) -> AccessPolicy | None:
    """读取策略；文件不存在返回None，由调用方按默认拒绝处理。"""
    path = policy_path(directory, corpus_id)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return AccessPolicy.from_dict(data, expected_corpus_id=corpus_id)


def write_policy(directory: Path, policy: AccessPolicy) -> Path:
    """原子写入策略文件，避免半成品被读到。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = policy_path(directory, policy.corpus_id)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(policy.to_dict(), output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def default_policy(corpus_id: str, visibility: str) -> AccessPolicy:
    """新建语料的初始策略：公开语料放行所有已认证客户端，内部语料先拒绝所有人。

    内部语料不给默认放行是故意的：宁可新语料暂时没人能读，也不能默认把它开放出去。
    """
    if visibility == "public":
        return AccessPolicy(corpus_id, "public", (WILDCARD,), "公开开源资料，任何已认证客户端可读")
    return AccessPolicy(
        corpus_id, "internal", (), "内部资料：默认拒绝，需显式把client_id加进allowed_clients"
    )


class AccessRegistry:
    """按语料加载策略并做判定；缺策略=拒绝。"""

    def __init__(self, directory: Path, policies: dict[str, AccessPolicy] | None = None) -> None:
        self.directory = directory
        self._policies = policies or {}

    @classmethod
    def from_directory(cls, directory: Path) -> "AccessRegistry":
        policies: dict[str, AccessPolicy] = {}
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                policy = load_policy(directory, path.stem)
                if policy is not None:
                    policies[policy.corpus_id] = policy
        return cls(directory, policies)

    def policy(self, corpus_id: str) -> AccessPolicy | None:
        return self._policies.get(corpus_id)

    def check(self, corpus_id: str, client_id: str | None) -> bool:
        """默认拒绝：没有策略文件、未认证、未授权，都返回False。"""
        policy = self.policy(corpus_id)
        return bool(policy and policy.allows(client_id))

    def require(self, corpus_id: str, client_id: str | None) -> AccessPolicy:
        """不通过就抛AccessDenied；调用方据此返回403。"""
        policy = self.policy(corpus_id)
        if policy is None:
            raise AccessDenied(f"没有为语料 {corpus_id} 配置访问策略，默认拒绝")
        if not policy.allows(client_id):
            raise AccessDenied(f"客户端无权读取语料 {corpus_id}")
        return policy

    def visible_corpora(self, client_id: str | None) -> list[str]:
        return sorted(
            corpus_id for corpus_id, policy in self._policies.items()
            if policy.allows(client_id)
        )

    def refresh(self) -> "AccessRegistry":
        """重新读盘：策略变更后不需要重启服务。"""
        return AccessRegistry.from_directory(self.directory)
