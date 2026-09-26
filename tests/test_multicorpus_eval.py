from pathlib import Path

from evaluation.run_multicorpus_eval import dataset_label, evidence_libraries, route_arguments


def test_route_arguments_map_dataset_labels_to_route_ids():
    """题集里是展示名和带出处的版本串，必须能路由到候选索引。"""
    assert route_arguments("osgEarth", "3.8") == ("osgearth", "3.8")
    assert route_arguments("OGC WMS", "1.3.0 (OGC 06-042)") == ("ogc-wms", "1.3.0")
    assert route_arguments("PROJ", "9.8.1") == ("proj", "9.8.1")


def test_evidence_libraries_flags_cross_library_questions():
    spans = [
        {"path": "raw_data/osgearth/3.8/docs/source/wms.md"},
        {"path": "raw_data/ogc-wms/1.3.0/06-042.pdf"},
    ]
    assert evidence_libraries(spans) == ["ogc-wms", "osgearth"]


def test_dataset_label_survives_a_dataset_outside_the_given_root():
    """题集在给定 root 之外时不能抛异常——那会让**跑完的一整轮评测作废**。

    `--root` 指向临时根、`--dataset` 仍取仓库那份，是评测临时根候选时的常规用法。
    而这个标识是在 50 题全跑完、模型也释放之后才写的，为它崩掉等于白跑。
    """
    root = Path("E:/tmp/fake-root")
    assert dataset_label(
        Path("evaluation/datasets/rag-validation-001.jsonl"), root=root
    ) == "rag-validation-001.jsonl"


def test_dataset_label_is_relative_to_the_given_root():
    """落在 root 之内时给相对路径，报告里不出现绝对路径。"""
    root = Path("E:/tmp/fake-root")
    assert dataset_label(
        root / "evaluation/datasets/rag-validation-001.jsonl", root=root
    ) == "evaluation/datasets/rag-validation-001.jsonl"
