"""管理语料访问策略：列出、补齐、授权、撤权。

访问策略存放在 configs/access/<corpus-id>.json，不进Git（属于部署配置）。
默认拒绝：没有策略文件的语料，检索、列表和取件全部不放行。

运行示例：
  uv run python scripts/manage_access.py --list
  uv run python scripts/manage_access.py --sync
  uv run python scripts/manage_access.py --grant mylib-1.0 --client agent-a
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wenjin.access import (  # noqa: E402
    WILDCARD,
    AccessPolicy,
    AccessRegistry,
    default_policy,
    load_policy,
    write_policy,
)
from wenjin.catalog import corpus_catalog  # noqa: E402

ACCESS_DIR = ROOT / "configs/access"


def list_policies() -> int:
    catalog = {entry.corpus_id: entry for entry in corpus_catalog(ROOT)}
    registry = AccessRegistry.from_directory(ACCESS_DIR)
    print(f"{'语料':32s} {'可见性':9s} {'允许的客户端':28s} 索引")
    for corpus_id, entry in sorted(catalog.items()):
        policy = registry.policy(corpus_id)
        if policy is None:
            print(f"{corpus_id:32s} {'-':9s} {'【无策略：默认拒绝】':28s} "
                  f"{'有' if entry.indexed else '无'}")
            continue
        clients = ",".join(policy.allowed_clients) or "（空：拒绝所有人）"
        print(f"{corpus_id:32s} {policy.visibility:9s} {clients:28s} "
              f"{'有' if entry.indexed else '无'}")
    return 0


def sync_policies(*, dry_run: bool) -> int:
    """为还没有策略的语料生成默认策略：公开语料放行，内部语料先拒绝所有人。"""
    created = 0
    for entry in corpus_catalog(ROOT):
        if load_policy(ACCESS_DIR, entry.corpus_id) is not None:
            continue
        policy = default_policy(entry.corpus_id, entry.visibility)
        clients = ",".join(policy.allowed_clients) or "（空）"
        print(f"新建策略 {entry.corpus_id}：visibility={policy.visibility} clients={clients}")
        if not dry_run:
            write_policy(ACCESS_DIR, policy)
        created += 1
    if not created:
        print("所有语料都已有访问策略。")
    elif dry_run:
        print("（--dry-run：未写盘）")
    else:
        print(f"已写入 {ACCESS_DIR.relative_to(ROOT)}。内部语料默认拒绝，"
              "请用 --grant 把 client_id 加进去。")
    return 0


def change_client(corpus_id: str, client_id: str, *, grant: bool) -> int:
    policy = load_policy(ACCESS_DIR, corpus_id)
    if policy is None:
        print(f"语料 {corpus_id} 还没有策略，请先运行 --sync。")
        return 1
    clients = set(policy.allowed_clients)
    if grant:
        clients.add(client_id)
    else:
        clients.discard(client_id)
    clients.discard(WILDCARD)
    updated = AccessPolicy(corpus_id, policy.visibility, tuple(sorted(clients)), policy.note)
    write_policy(ACCESS_DIR, updated)
    shown = ",".join(updated.allowed_clients) or "（空：拒绝所有人）"
    print(f"{corpus_id} 允许的客户端现在是：{shown}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="管理语料访问策略")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出语料和现有策略")
    group.add_argument("--sync", action="store_true", help="为缺失的语料生成默认策略")
    group.add_argument("--grant", metavar="CORPUS_ID", help="给某个语料加一个客户端")
    group.add_argument("--revoke", metavar="CORPUS_ID", help="从某个语料移除一个客户端")
    parser.add_argument("--client", help="--grant/--revoke 需要的 client_id")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    if arguments.list:
        raise SystemExit(list_policies())
    if arguments.sync:
        raise SystemExit(sync_policies(dry_run=arguments.dry_run))
    if not arguments.client:
        parser.error("--grant/--revoke 需要 --client")
    raise SystemExit(change_client(arguments.grant or arguments.revoke, arguments.client,
                                   grant=bool(arguments.grant)))


if __name__ == "__main__":
    main()
