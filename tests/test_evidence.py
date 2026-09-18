from types import SimpleNamespace

import pytest

from wenjin.evidence import OpenAIEvidenceJudge, parse_evidence_assessment


def test_parse_supported_evidence():
    result = parse_evidence_assessment(
        '{"status":"supported","citations":[1],"supported_claims":["run loops"],'
        '"missing_aspects":[],"reason":"证据明确"}',
        available_ranks={1, 2},
    )
    assert result.status == "supported"
    assert result.citations == (1,)


def test_reject_unknown_citation_and_inconsistent_supported_status():
    with pytest.raises(ValueError, match="不存在"):
        parse_evidence_assessment(
            '{"status":"insufficient","citations":[9],"supported_claims":[],'
            '"missing_aspects":["API"],"reason":"缺失"}',
            available_ranks={1},
        )
    with pytest.raises(ValueError, match="不能同时"):
        parse_evidence_assessment(
            '{"status":"supported","citations":[1],"supported_claims":[],'
            '"missing_aspects":["返回值"],"reason":"冲突"}',
            available_ranks={1},
        )


def test_openai_compatible_judge_can_be_injected():
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=(
        '{"status":"partially_supported","citations":[1],'
        '"supported_claims":["返回NULL"],"missing_aspects":["错误码"],"reason":"只支持一部分"}'
    )))])

    class Completions:
        def create(self, **kwargs):
            assert kwargs["model"] == "fake"
            return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    judge = OpenAIEvidenceJudge(client=client, model="fake")
    evidence = [{"rank": 1, "version": "3.6.5", "source_path": "x",
                 "start_line": 1, "end_line": 2, "text": "return NULL"}]
    assert judge.assess("返回错误码吗", evidence).status == "partially_supported"
