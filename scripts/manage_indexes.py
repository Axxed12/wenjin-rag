"""验证、切换和回退active索引；切换后重启MCP加载新目标。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REGISTRY_PATH = ROOT / "data/indexes/index-registry.json"


def validate_with_client(target):
    from qdrant_client import QdrantClient

    from wenjin.index_registry import validate_target
    from wenjin.settings import Settings

    settings = Settings()
    if settings.embedding_model_path.name != target.embedding_model:
        raise ValueError(
            f"当前Embedding模型{settings.embedding_model_path.name}与目标"
            f"{target.embedding_model}不一致"
        )
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        return validate_target(target, root=ROOT, qdrant_client=client)
    finally:
        client.close()


def main() -> None:
    from wenjin.index_registry import IndexRegistryStore, load_target

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("target", type=Path)
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("target", type=Path)
    init_parser.add_argument("--reason", required=True)
    switch_parser = subparsers.add_parser("switch")
    switch_parser.add_argument("target", type=Path)
    switch_parser.add_argument("--reason", required=True)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    store = IndexRegistryStore(REGISTRY_PATH)
    if args.command == "status":
        result = store.load()
    elif args.command == "validate":
        result = validate_with_client(load_target(args.target))
    elif args.command == "init":
        target = load_target(args.target)
        validation = validate_with_client(target)
        state = store.initialize(target, reason=args.reason)
        result = {"validation": validation, "registry": state}
    elif args.command == "switch":
        target = load_target(args.target)
        validation = validate_with_client(target)
        state = store.switch(target, reason=args.reason)
        result = {
            "validation": validation,
            "registry": state,
            "next_step": "restart MCP service and run smoke query",
        }
    else:
        target = store.previous()
        validation = validate_with_client(target)
        state = store.rollback(reason=args.reason)
        result = {
            "validation": validation,
            "registry": state,
            "next_step": "restart MCP service and run smoke query",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
