from scripts.query_dense_index import format_human_result


def test_human_result_is_compact_and_keeps_source():
    output = format_human_result(
        "Geode 如何添加 Drawable？",
        "blocks",
        [
            {
                "score": 0.7,
                "source_path": "include/osg/Geode",
                "start_line": 42,
                "end_line": 63,
                "version": "3.6.5",
                "strategy": "blocks",
                "chunk_id": "one",
                "text": "  Add a Drawable\n virtual bool addDrawable(Drawable* drawable); ",
            }
        ],
    )
    assert "score=0.70000" in output
    assert "include/osg/Geode:42-63" in output
    assert "Add a Drawable virtual bool" in output
    assert "不是答案正确率" in output
