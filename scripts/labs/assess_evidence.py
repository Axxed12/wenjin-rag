"""检索公开OSG问题并用已配置的OpenAI兼容模型审核证据支持性。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from wenjin.evidence import OpenAIEvidenceJudge
    from wenjin.mcp_server import create_default_service
    from wenjin.retrieval.query_rewrite import QueryRewriteSettings
    from wenjin.retrieval.service import SearchConfig

    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="仅用于公开、非敏感问题；证据会发送给配置的模型API")
    args = parser.parse_args()
    service = create_default_service()
    try:
        result = service.search(args.query, config=SearchConfig(top_k=5))
        try:
            assessment = OpenAIEvidenceJudge(QueryRewriteSettings()).assess(
                args.query, result["evidence"]
            )
            output = {
                "status": assessment.status,
                "safe_to_answer_fully": assessment.status == "supported",
                "citations": list(assessment.citations),
                "supported_claims": list(assessment.supported_claims),
                "missing_aspects": list(assessment.missing_aspects),
                "reason": assessment.reason,
                "judge_error_type": None,
            }
        except Exception as exc:
            output = {
                "status": "unverified",
                "safe_to_answer_fully": False,
                "citations": [],
                "supported_claims": [],
                "missing_aspects": ["evidence judge failed"],
                "reason": "审核失败时保守降级，不允许生成完整答案",
                "judge_error_type": type(exc).__name__,
            }
    finally:
        service.close()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
