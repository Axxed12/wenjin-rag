from types import SimpleNamespace

import pytest

from wenjin.retrieval.query_rewrite import (
    OpenAIQueryRewriter,
    build_rewrite_messages,
    parse_rewrite_response,
    rewrite_or_fallback,
)


def test_parse_rewrite_response_and_deduplicate_queries():
    result = parse_rewrite_response(
        "Geode 能否调用 addChild？",
        '''```json
        {
          "english_query": "Can Geode call addChild?",
          "subqueries": ["What is Geode's base class?", "Can Geode call addChild?"]
        }
        ```''',
    )
    assert result.english_query == "Can Geode call addChild?"
    assert result.retrieval_queries() == [
        "Geode 能否调用 addChild？",
        "Can Geode call addChild?",
        "What is Geode's base class?",
    ]


def test_invalid_model_json_is_rejected():
    with pytest.raises(ValueError, match="subqueries"):
        parse_rewrite_response(
            "问题",
            '{"english_query": "question", "subqueries": "not-a-list"}',
        )


def test_prompt_keeps_query_in_user_json_message():
    messages = build_rewrite_messages(
        "OSG 3.6.5 中怎样加载纹理？",
        corpus_hint="OSG C++ source",
    )
    assert "加载纹理" not in messages[0]["content"]
    assert "加载纹理" in messages[1]["content"]


def test_openai_compatible_client_can_be_injected():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=(
                        '{"english_query":"How to add a child?",'
                        '"subqueries":["Where is addChild declared?"]}'
                    )
                )
            )
        ]
    )

    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["model"] == "fake-model"
            return response

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    rewriter = OpenAIQueryRewriter(client=fake_client, model="fake-model")
    result = rewriter.rewrite("如何添加子节点？", corpus_hint="OSG source")
    assert result.subqueries == ("Where is addChild declared?",)


def test_model_failure_falls_back_to_original_query():
    class BrokenRewriter:
        def rewrite(self, *args, **kwargs):
            raise TimeoutError("secret upstream detail")

    result, error_type = rewrite_or_fallback(
        BrokenRewriter(),
        "原问题",
        corpus_hint="OSG source",
    )
    assert result.retrieval_queries() == ["原问题"]
    assert error_type == "TimeoutError"
