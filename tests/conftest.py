"""按测试依赖边界标记pytest合同，不改变默认测试选择。"""

from pathlib import Path

import pytest

# quick仅用本地数据/mock；integration跨多个本地模块或隔离服务；live触碰冻结集/真实资产。
_TEST_MARKERS = {
    'test_3dtiles_longline_checkpoint.py': "quick",
    'test_access_and_paths.py': "quick",
    'test_audit.py': "quick",
    'test_bm25.py': "quick",
    'test_chunk_coverage.py': "quick",
    'test_chunking.py': "quick",
    'test_content_hashing.py': "quick",
    'test_context_selection_contract.py': "quick",
    'test_default_multicorpus_route.py': "quick",
    'test_evidence.py': "quick",
    'test_evidence_threshold.py': "quick",
    'test_external_corpus_audit.py': "quick",
    'test_fusion.py': "quick",
    'test_index_audit.py': "quick",
    'test_index_registry.py': "quick",
    'test_ingest.py': "quick",
    'test_iteration_retrieval_validation.py': "quick",
    'test_line_preserving_comparison.py': "quick",
    'test_long_line_evidence_contract.py': "quick",
    'test_managed_mcp_http_client.py': "quick",
    'test_modify_delete_package.py': "quick",
    'test_multicorpus_eval.py': "quick",
    'test_multiformat_parsing.py': "quick",
    'test_ocr_pipeline.py': "quick",
    'test_parsers.py': "quick",
    'test_qdrant_connection.py': "quick",
    'test_query_expansion.py': "quick",
    'test_query_format.py': "quick",
    'test_query_rewrite.py': "quick",
    'test_recover_table.py': "quick",
    'test_rerank.py': "quick",
    'test_retrieval_metrics.py': "quick",
    'test_source_context.py': "quick",
    'test_table_text.py': "quick",
    'test_admin_api.py': "integration",
    'test_admin_snapshot_consistency.py': "integration",
    'test_auth.py': "integration",
    'test_call_logs.py': "integration",
    'test_candidate_build.py': "integration",
    'test_catalog_fault_isolation.py': "integration",
    'test_file_management_api_entry.py': "integration",
    'test_file_management_service.py': "integration",
    'test_legacy_search.py': "integration",
    'test_managed_mcp_integration.py': "integration",
    'test_mcp_multicorpus.py': "integration",
    'test_mcp_server.py': "integration",
    'test_phase1_source_replay_contract.py': "integration",
    'test_release_isolation.py': "integration",
    'test_retrieval_service.py': "integration",
    'test_review_fixes.py': "integration",
    'test_router.py': "integration",
    'test_shared_model_concurrency.py': "integration",
    'test_source_context_service_integration.py': "integration",
    'test_source_endpoint.py': "integration",
    'test_source_text_audit.py': "integration",
    'test_zh_segmentation.py': "integration",
    'test_frozen_dataset.py': "live",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """按已审模块映射加marker；新模块也可用pytestmark显式声明运行边界。"""
    test_items = [item for item in items if Path(str(item.path)).name.startswith("test_")]
    allowed = {"quick", "integration", "live"}
    unclassified = sorted(
        {
            Path(str(item.path)).name
            for item in test_items
            if Path(str(item.path)).name not in _TEST_MARKERS
            and not any(marker.name in allowed for marker in item.iter_markers())
        }
    )
    if unclassified:
        raise pytest.UsageError(f"tests need quick/integration/live classification: {unclassified}")
    for item in test_items:
        # 函数级成本标记优先于文件名映射，便于一个模块里混合不同成本的合同。
        # 模块级标记则是整份模块的声明，也不再叠加文件名默认值。
        if any(marker.name in allowed for marker in item.own_markers):
            continue
        if any(marker.name in allowed for marker in item.iter_markers()):
            continue
        category = _TEST_MARKERS.get(Path(str(item.path)).name)
        if category is not None:
            item.add_marker(getattr(pytest.mark, category))
