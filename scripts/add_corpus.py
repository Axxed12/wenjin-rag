"""CLI：把本地文档加入语料库。

这里只做编排和输出：真正的入库逻辑在 `wenjin.ingestion.intake`，OCR决策在
`wenjin.ingestion.ocr`。这样批处理脚本、将来的服务入口都能复用同一套逻辑，
不必导入本脚本。

运行示例：
  uv run python scripts/add_corpus.py --library mylib --version 1.0 --source ./docs \
      --license MIT --source-url https://example.org/docs --dry-run
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wenjin.ingestion import intake  # noqa: E402


def interactive_ask(prompt: str) -> bool:
    """CLI的确认方式；输入被关闭时不能当成同意。"""
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        raise SystemExit(
            "没有收到确认输入。请显式选择 --ocr always 或 --ocr never，也可以加 --yes。"
        ) from None
    return answer in {"y", "yes"}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把本地文档加入语料库并建候选索引")
    parser.add_argument("--library", required=True, help="库名，例如 mylib")
    parser.add_argument("--version", required=True, help="版本，例如 1.0")
    parser.add_argument("--source", type=Path, required=True, help="文件或目录")
    parser.add_argument("--license", default="unknown", help="资料许可；未知时如实写 unknown")
    parser.add_argument("--source-url", help="上游出处URL；缺省时记为本地urn")
    parser.add_argument("--intranet-url", help="内网知识库地址")
    parser.add_argument("--visibility", choices=["public", "internal"], default="internal",
                        help="public写corpora/（会提交Git），internal写corpora-private/")
    parser.add_argument("--ocr", choices=["ask", "always", "never"], default="ask")
    parser.add_argument("--yes", action="store_true", help="非交互：需要OCR时直接同意")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写盘、不建索引")
    parser.add_argument("--no-build", action="store_true", help="只写manifest和原文，不建索引")
    parser.add_argument("--strategy", default="blocks-split", choices=["blocks-split", "blocks"])
    return parser.parse_args()


def resolve_paths(arguments: argparse.Namespace) -> tuple[str, str, str, Path, Path, Path]:
    from wenjin.corpus_identity import validate_identifier

    # library/version会进入corpus_id和磁盘路径，必须是安全标识符：
    # 拒绝绝对路径、分隔符、`..`和平台特殊路径，避免越出语料目录。
    library = validate_identifier(arguments.library, field="library")
    version = validate_identifier(arguments.version, field="version")
    corpus_id = f"{library}-{version}"
    base = ROOT / ("corpora" if arguments.visibility == "public" else "corpora-private")
    manifest_path = base / corpus_id / "source-manifest.json"
    raw_root = ROOT / "raw_data" / library / version
    if manifest_path.exists():
        raise SystemExit(f"语料已存在，拒绝覆盖：{manifest_path}")
    return corpus_id, library, version, manifest_path, raw_root


def print_plan(plan: intake.CorpusPlan) -> None:
    print(f"语料：{plan.corpus_id}，来源：{plan.source}")
    print(f"共 {len(plan.files)} 个文件（可检索 {len(plan.eligible)}，仅归档 {len(plan.archived)}）")
    for item in plan.files:
        state = "可检索" if item.eligible else "仅归档"
        detail = item.parser or "；".join(item.notes) or "-"
        print(f"  [{state}] {item.relative} ({item.size}字节, {detail})")


def ensure_access_policy(corpus_id: str, visibility: str) -> None:
    """为新语料写一份初始访问策略；不覆盖已有的。

    公开语料放行所有已认证客户端；内部语料先拒绝所有人——宁可新语料暂时没人能读，
    也不能默认把它开放出去。
    """
    from wenjin.access import default_policy, load_policy, write_policy

    access_dir = ROOT / "configs/access"
    if load_policy(access_dir, corpus_id) is not None:
        return
    policy = default_policy(corpus_id, visibility)
    write_policy(access_dir, policy)
    if policy.allowed_clients:
        print(f"访问策略：{corpus_id} 可见性={visibility}，放行所有已认证客户端")
    else:
        print(f"访问策略：{corpus_id} 目前拒绝所有人（内部语料默认不开放）。")
        print(f"  授权某个客户端：uv run python scripts/manage_access.py "
              f"--grant {corpus_id} --client <client_id>")


def build_index(manifest_path: Path, arguments: argparse.Namespace, *,
                corpus_id: str, library: str, version: str, visibility: str):
    """建独立候选索引；不切换active，只打印切换命令。

    library/version用校验后的值（validate_identifier会去掉首尾空白），
    不能用原始参数，否则collection名可能和corpus_id对不上。
    """
    from qdrant_client import QdrantClient

    from wenjin.index_registry import validate_target
    from wenjin.indexing.build import build_candidate_index
    from wenjin.settings import Settings

    settings = Settings()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    suffix = arguments.strategy.replace("-", "_")
    collection = f"{library}_{version}_dense_{suffix}".replace(".", "_")
    bm25 = ROOT / "data/indexes" / f"{collection}_bm25.sqlite"
    target_id = f"{corpus_id}-{arguments.strategy}"
    try:
        target = build_candidate_index(
            manifest_path=manifest_path, target_id=target_id, strategy=arguments.strategy,
            dense_collection=collection, bm25_path=bm25, qdrant_client=client,
            embedder=settings.embedding(),
            embedding_model=settings.embedding_model_path.name, project_root=ROOT,
        )
        validate_target(target, root=ROOT, qdrant_client=client)
    finally:
        client.close()
    # 内部语料的配置写到被忽略的目录：配置里有内部库名和版本，
    # 放进可提交目录等于绕过 `corpora-private/` 的忽略规则把它泄露出去。
    from wenjin.catalog import index_config_dir

    config_path = index_config_dir(ROOT, visibility=visibility) / f"{target_id}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(asdict(target), ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    print(f"\n候选索引已建：{target.chunk_count} chunk，collection={collection}")
    print(f"索引配置：{config_path.relative_to(ROOT)}")
    print("候选未切换为active。确认检索效果后手动切换：")
    print(f"  uv run python scripts/manage_indexes.py switch {config_path.relative_to(ROOT)}"
          ' --reason "..."')


def plan_pdfs(inspections, *, ocr_mode: str, assume_yes: bool):
    """决定每个PDF的解析安排，并把"哪些页不会被索引"显式说出来。

    返回 (pdf_plans, excluded)。检测事实随 plan 写进 manifest，
    入库之后不必重跑检测也能复核当时的选择。
    """
    from wenjin.ingestion.ocr import (
        check_mineru,
        choose_pdf_parser,
        decide_ocr,
        describe_scan_need,
        skipped_scan_pages,
    )

    # 没有扫描页时连探测都不做：普通文件入库不应该依赖MinerU是否在线。
    if any(item.needs_ocr for item in inspections):
        decision = decide_ocr(
            inspections, status=check_mineru(), mode=ocr_mode,
            assume_yes=assume_yes, ask=interactive_ask,
        )
    else:
        decision = decide_ocr(inspections, status=None)
    for message in decision.messages:
        print(message)

    plans = {}
    for item in inspections:
        parser = choose_pdf_parser(item, enabled=decision.enabled)
        skipped = skipped_scan_pages(item, parser)
        plans[item.path] = intake.PdfPlan(parser=parser, report=item.to_report(),
                                          skipped_scan_pages=tuple(skipped))
        if skipped:
            print(f"注意：{item.path.name} 第 {skipped} 页没有可用文字层且未启用OCR，"
                  "这些页不会被索引。")
    # describe_scan_need 在这里再报一次：上面只说了检测事实，这里说清实际执行了什么。
    if decision.enabled:
        for line in describe_scan_need(inspections):
            print(f"OCR目标：{line}")
    return plans, decision.excluded


def main() -> None:
    arguments = parse_arguments()

    source = arguments.source.resolve()
    corpus_id, library, version, manifest_path, raw_root = resolve_paths(arguments)

    inspections = intake.scan_pdfs(intake.collect_files(source))
    pdf_plans, excluded = plan_pdfs(
        inspections, ocr_mode=arguments.ocr, assume_yes=arguments.yes
    )
    plan = intake.plan_corpus(
        source=source, library=library, version=version,
        corpus_id=corpus_id,
        pdf_plans=pdf_plans,
        excluded=excluded,
    )
    print_plan(plan)
    manifest = intake.build_manifest(
        plan, license_text=arguments.license, source_url=arguments.source_url,
        intranet_url=arguments.intranet_url,
    )
    if arguments.dry_run:
        print("\n--dry-run：未写盘、未建索引。")
        return

    written = intake.materialize(plan, raw_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    print(f"\nmanifest：{manifest_path.relative_to(ROOT)}")
    print(f"原文目录：{raw_root.relative_to(ROOT)}（新写入 {len(written)} 个文件）")
    ensure_access_policy(corpus_id, arguments.visibility)

    counts = intake.retrieve_chunk_counts(manifest_path, ROOT / "raw_data",
                                          strategy=arguments.strategy)
    empty = intake.empty_retrieval_entries(manifest, counts)
    if empty:
        raise SystemExit(
            f"以下文件标为可检索但没有切出任何chunk，拒绝入库：{empty}。"
            "请检查文件是否为空，或对扫描件使用 --ocr always。"
        )
    print(f"切片检查通过：{sum(counts.values())} 个chunk（{len(counts)} 个文档单元）")

    if arguments.no_build:
        print("\n--no-build：已跳过索引构建。")
        return
    build_index(manifest_path, arguments, corpus_id=corpus_id,
                library=library, version=version, visibility=arguments.visibility)


if __name__ == "__main__":
    main()
