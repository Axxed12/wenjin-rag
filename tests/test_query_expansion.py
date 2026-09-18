from wenjin.retrieval.query_expansion import code_identifier_terms, expand_query, source_routes


def test_texture_query_expansion_is_explicit():
    expanded, added = expand_query("将图片加载为二维纹理并绑定到纹理单元 0")
    assert "Texture2D" in added
    assert "readRefImageFile" in added
    assert "setTextureAttributeAndModes" in added
    assert expanded.startswith("将图片加载")


def test_identifier_terms_select_code_shapes():
    assert code_identifier_terms(["OSG", "Geode", "addChild", "ref_ptr"]) == [
        "addChild",
        "ref_ptr",
    ]


def test_source_route_uses_explicit_example_intent():
    assert source_routes("找一个官方源码片段") == ["examples/%"]
    assert source_routes("接口 API 是什么") == ["include/%"]
    assert source_routes("随便问问") == []
