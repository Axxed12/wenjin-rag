"""补跑 RAGAS 的 AnswerRelevancy，并做判分可靠性验证。

为什么单独一个脚本
------------------
`run_ragas_eval.py` 跑的是不需要生成答案的检索侧指标，它的 `metrics_not_run`
里写着 AnswerRelevancy「需要 embedding 模型，环境里未安装」。补这一项要额外做两件事，
塞进原脚本会把已经定稿的那份报告口径搅浑：

1. 复用**已生成并留存的答案**，不重新生成。答案就在
   `artifacts/frozen-test-001/ragas-retrieval-eval.json` 的 `records[].answer` 里，
   是 `xopqwen35397b` 按 `ANSWER_PROMPT` 生成的同一批。重生成一遍就是换口径。
2. 判分可靠性验证。项目对 `faithfulness` 判定不可信，靠的是两个受控输入；这里对
   AnswerRelevancy 做同样的事（受控输入 + 同一输入连判两次）。

不重新检索
----------
题干与上下文仍走 `run_ragas_eval.build_samples`：它只读
`test-retrieval-eval.json` 里**已记录**的 rerank top-k chunk ID，再从 BM25 库取正文。
不碰 Qdrant、不重建索引、不改语料。

判分与 embedding
----------------
- 判分：与既有三个指标同一套 `RAGAS_JUDGE_*`（DeepSeek `deepseek-flash`），
  `max_tokens=4096`、`temperature=0`、思考模式不关——与报告里记录的 judge 配置逐项一致。
- embedding：`evaluation/ragas/` 环境原本没装 `sentence_transformers`，本脚本依赖它。
  模型用**项目自己检索时用的那个**本地 Qwen3-Embedding-0.6B
  （`EMBEDDING_MODEL_PATH`），加载参数与 `src/wenjin/settings.py` 一致：
  归一化、不做 query instruction 前缀。全程离线，不连 huggingface.co。

受控验证怎么读
--------------
AnswerRelevancy 只看 `user_input` 与 `response`：让判分模型从答案倒推 3 个问题，
再算这 3 个问题与原问题的余弦相似度均值；若 3 个问题被**全部**判为 noncommittal，
分数被直接乘 0。所以 0 分有两种成因，脚本把中间量（生成的问题、noncommittal 标记、
未乘系数的余弦均值）一并留下，好分辨是「答非所问」还是「被 noncommittal 归零」。

运行：
    uv run --project evaluation/ragas python evaluation/quality/run_answer_relevancy_eval.py --limit 3
    uv run --project evaluation/ragas python evaluation/quality/run_answer_relevancy_eval.py
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ["RAGAS_DO_NOT_TRACK"] = "true"
# 这台机器上 huggingface.co 不通；本地模型走文件路径，显式置离线以免任何回源尝试。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用既有脚本的口径定义与取数逻辑，避免两处各写一份而慢慢漂移。
from run_ragas_eval import (  # noqa: E402
    ANSWER_PROMPT,
    DEFAULT_DATASET,
    DEFAULT_RETRIEVAL,
    DEFAULT_ROUTE,
    bm25_paths,
    build_samples,
    load_env,
)

DEFAULT_SOURCE = ROOT / "artifacts/frozen-test-001/ragas-retrieval-eval.json"
DEFAULT_OUTPUT = ROOT / "artifacts/frozen-test-001/ragas-answer-relevancy.json"

# 受控输入：对照组要能区分「判分在正常工作」和「判分在乱给分」。
CONTROL_RESPONSE = {
    # 标准答案：人写的 rubric 就是针对这个问题的答案，相关性应当接近满分。
    "gold_rubric": "reference",
    # 检索到的上下文原文本身。它包含答案所需内容，但不是「回答问题」的措辞，
    # AnswerRelevancy 只看 response 本身像不像在回答，所以这一组是**下界**式的对照。
    "retrieved_context": "context",
}


def load_saved_answers(source_path: Path) -> dict[str, dict]:
    """从既有报告里取回已生成的答案；不重新生成。"""
    report = json.loads(source_path.read_text(encoding="utf-8"))
    answers = {}
    for record in report["records"]:
        if record.get("answer"):
            answers[record["id"]] = record["answer"]
    return report, answers


def describe(values: list[float]) -> dict:
    """数值一律带标准差：只看均值没法判断结论稳不稳。"""
    if not values:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        # 样本标准差（n-1）；n=1 时无定义，返回 None 而不是 0。
        "std": round(statistics.stdev(values), 4) if len(values) > 1 else None,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def build_embeddings(model_path: str, batch_size: int = 16):
    """把项目检索用的那个本地 embedding 模型包成 langchain Embeddings。

    为什么不用 ragas 自带的 `ragas.embeddings.HuggingFaceEmbeddings`：ragas 0.4.3 里
    它是**新接口**（`BaseRagasEmbedding`，只有 `embed_text`），而 `AnswerRelevancy`
    的 `calculate_similarity` 走的是**旧接口**（要 `embed_query` / `embed_documents`）。
    直接传进去会 `AttributeError: 'HuggingFaceEmbeddings' object has no attribute
    'embed_query'`——版本内部不自洽，不是配置问题。

    加载方式与 `src/wenjin/settings.py` 的 embedder 对齐：归一化、批量 16、
    不加 query instruction 前缀。
    """
    from langchain_core.embeddings import Embeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from sentence_transformers import SentenceTransformer

    class LocalSentenceTransformer(Embeddings):
        def __init__(self) -> None:
            # 不能叫 self.model：LangchainEmbeddingsWrapper 会拿它的 repr 当遥测里的
            # 模型名，非字符串会让 EmbeddingUsageEvent 校验失败。
            self._encoder = SentenceTransformer(model_path, device="cpu")

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            vectors = self._encoder.encode(
                list(texts), normalize_embeddings=True, batch_size=batch_size
            )
            return [vector.tolist() for vector in vectors]

        def embed_query(self, text: str) -> list[float]:
            return self.embed_documents([text])[0]

    return LangchainEmbeddingsWrapper(LocalSentenceTransformer())


def build_metric(llm, embeddings, strictness: int):
    """带诊断的 AnswerRelevancy：分数之外留下生成的问题与 noncommittal 标记。

    不重写判分逻辑——`_ascore` 照抄上游实现，只是在返回前把中间量记下来。
    上游若改了实现，这里的诊断会失真，但分数仍走上游的 `_calculate_score`。
    """
    import numpy as np
    from ragas.metrics._answer_relevance import AnswerRelevancy, ResponseRelevanceInput

    class DiagnosedAnswerRelevancy(AnswerRelevancy):
        async def _ascore(self, row, callbacks):
            prompt_input = ResponseRelevanceInput(response=row["response"])
            responses = await self.question_generation.generate_multiple(
                data=prompt_input, llm=self.llm, callbacks=callbacks, n=self.strictness
            )
            questions = [item.question for item in responses]
            noncommittal = [item.noncommittal for item in responses]
            raw = None
            if questions and not all(question == "" for question in questions):
                raw = float(np.mean(self.calculate_similarity(row["user_input"], questions)))
            self.last = {
                "generated_questions": questions,
                "noncommittal": noncommittal,
                "n_generated": len(questions),
                "raw_cosine_mean": None if raw is None else round(raw, 4),
                # 全部判为 noncommittal 时上游把分数乘 0，这是 0 分的一种成因。
                "zeroed_by_noncommittal": bool(noncommittal) and all(noncommittal),
            }
            return self._calculate_score(responses, row)

    return DiagnosedAnswerRelevancy(llm=llm, embeddings=embeddings, strictness=strictness)


async def run_pass(
    samples: list[dict], *, metric, response_of, label: str, delay: float
) -> list[dict]:
    """跑一轮判分；`response_of` 决定这一轮把什么当答案喂进去。"""
    from ragas import SingleTurnSample

    records = []
    for index, sample in enumerate(samples, start=1):
        response = response_of(sample)
        ragas_sample = SingleTurnSample(
            user_input=sample["user_input"],
            response=response,
            retrieved_contexts=sample["retrieved_contexts"],
            reference=sample["reference"],
        )
        record = {"id": sample["id"], "expected_status": sample["expected_status"]}
        started = time.perf_counter()
        try:
            value = await metric.single_turn_ascore(ragas_sample)
            record["score"] = None if value is None else round(float(value), 4)
        except Exception as error:  # noqa: BLE001 - 单题失败不该中断整轮，但要如实记录
            record["score"] = None
            record["error"] = f"{type(error).__name__}: {str(error)[:160]}"
        record["seconds"] = round(time.perf_counter() - started, 2)
        record.update(getattr(metric, "last", {}) or {})
        records.append(record)
        print(f"  [{label} {index:2d}/{len(samples)}] {sample['id']:14s} "
              f"score={record['score']}", flush=True)
        if delay:
            await asyncio.sleep(delay)
    return records


def compare(first: list[dict], second: list[dict]) -> dict:
    """两轮同一输入的逐题差；这是「判分噪声」的直接度量。"""
    index = {record["id"]: record for record in second}
    differences, changed, both = [], [], []
    for record in first:
        other = index.get(record["id"])
        if not other or record["score"] is None or other["score"] is None:
            continue
        both.append((record["id"], record["score"], other["score"]))
        difference = abs(record["score"] - other["score"])
        differences.append(difference)
        if difference > 0:
            changed.append({"id": record["id"], "first": record["score"],
                            "second": other["score"], "abs_diff": round(difference, 4)})
    return {
        "paired": len(both),
        "changed_questions": len(changed),
        "mean_abs_diff": round(statistics.fmean(differences), 4) if differences else None,
        "max_abs_diff": round(max(differences), 4) if differences else None,
        "changed": changed,
    }


def verdict_of(records: dict, repeat) -> dict:
    """按固定规则给结论，避免"看数下菜"。

    规则只有两条，任一不成立即判定不可信：

    A. 参考 rubric（人写的标准答案）的平均分必须**不低于**被测答案。若标准答案反而更低，
       这个指标就不能用来判断答案好坏——它连标准答案都认不出来。
    B. 标准答案必须赢过被测答案的题数占多数（>50%）。只看均值可能被少数极端题带偏。

    另外记录链路自检（echo 对照）与波动，作为结论的上下文，不参与判定。
    """
    main = [r["score"] for r in records["main"] if r.get("score") is not None]
    gold = [r["score"] for r in records["control_gold_rubric"] if r.get("score") is not None]
    main_by_id = {r["id"]: r["score"] for r in records["main"] if r.get("score") is not None}
    gold_by_id = {r["id"]: r["score"] for r in records["control_gold_rubric"]
                  if r.get("score") is not None}
    paired = [qid for qid in main_by_id if qid in gold_by_id]
    gold_wins = sum(1 for qid in paired if gold_by_id[qid] > main_by_id[qid])
    gold_win_rate = round(gold_wins / len(paired), 4) if paired else None

    # 原始余弦（未乘 noncommittal 系数）才是 embedding 链路的真实输出。
    echo_raw = [r["raw_cosine_mean"] for r in records["control_question_echo"]
                if r.get("raw_cosine_mean") is not None]
    chain_ok = bool(echo_raw) and statistics.fmean(echo_raw) >= 0.85

    rule_a = bool(main and gold) and statistics.fmean(gold) >= statistics.fmean(main)
    rule_b = gold_win_rate is not None and gold_win_rate > 0.5
    trusted = bool(rule_a and rule_b and chain_ok)

    return {
        "trusted": trusted,
        "rules": {
            "A_reference_rubric_not_below_generated": rule_a,
            "B_reference_rubric_wins_majority": rule_b,
            "C_embedding_chain_ok": chain_ok,
        },
        "evidence": {
            "generated_answer_mean": None if not main else round(statistics.fmean(main), 4),
            "reference_rubric_mean": None if not gold else round(statistics.fmean(gold), 4),
            "reference_rubric_wins_questions": f"{gold_wins}/{len(paired)}",
            "echo_control_raw_cosine_mean": (
                None if not echo_raw else round(statistics.fmean(echo_raw), 4)
            ),
            "repeat_mean_abs_diff": (
                None if repeat is None
                else compare(records["main"], repeat)["mean_abs_diff"]
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="补跑 AnswerRelevancy 并做判分可靠性验证")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--retrieval", type=Path, default=DEFAULT_RETRIEVAL)
    parser.add_argument("--route", type=Path, default=DEFAULT_ROUTE)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="已生成答案所在报告；答案从这里取，不重新生成")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--retriever", default="rerank",
                        choices=["dense", "bm25", "rrf", "rerank"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--strictness", type=int, default=3,
                        help="每题由答案倒推几个问题；RAGAS 默认 3")
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="与既有报告一致；推理模型给少了会静默返回空内容")
    parser.add_argument("--delay", type=float, default=0.0, help="每题之间的间隔秒数")
    parser.add_argument("--limit", type=int, help="只跑前 N 题，用于先验证链路")
    parser.add_argument("--skip-repeat", action="store_true",
                        help="跳过第二轮重复判分（省调用，但拿不到波动）")
    parser.add_argument("--dry-run", action="store_true", help="只组装样本，不调用模型")
    arguments = parser.parse_args()

    env = load_env(ROOT / ".env")
    source_report, answers = load_saved_answers(arguments.source)
    paths = bm25_paths(arguments.route)
    samples, missing = build_samples(
        arguments.dataset, arguments.retrieval, paths,
        retriever=arguments.retriever, top_k=arguments.top_k,
    )
    if arguments.limit:
        samples = samples[: arguments.limit]

    # 答案与题目按 id 对齐后再核对题干，防止拿错批次。
    lost = [sample["id"] for sample in samples if sample["id"] not in answers]
    if lost:
        raise SystemExit(f"这些题在既有报告里没有留存的答案：{lost}")
    for sample in samples:
        sample["saved_answer"] = answers[sample["id"]]
        sample["context_text"] = "\n\n".join(sample["retrieved_contexts"])

    print(f"样本 {len(samples)} 题；未取到正文 {len(missing)} 题；"
          f"检索器={arguments.retriever} top_k={arguments.top_k}")
    print(f"答案来源：{arguments.source.name}（既有报告，未重新生成）")
    if missing:
        print("  未取到正文:", [item["id"] for item in missing])
    if not samples:
        raise SystemExit("没有可用样本")

    if arguments.dry_run:
        absent = [key for key in
                  ("RAGAS_JUDGE_BASE_URL", "RAGAS_JUDGE_API_KEY", "RAGAS_JUDGE_MODEL")
                  if not env.get(key)]
        print("\n--- 判分配置 ---")
        print(f"  model={env.get('RAGAS_JUDGE_MODEL')} base_host="
              f"{(env.get('RAGAS_JUDGE_BASE_URL') or '').split('//')[-1].split('/')[0]}")
        print(f"  缺失项：{absent or '无'}")
        print("\n--- embedding ---")
        print(f"  {env.get('EMBEDDING_MODEL_PATH', '<未配置>')}")
        print("\n--- 会发出的调用次数 ---")
        passes = 4 + (0 if arguments.skip_repeat else 1)
        print(f"  AnswerRelevancy：{len(samples)} 题 x {arguments.strictness} 个问题 "
              f"= 约 {len(samples) * arguments.strictness} 次/轮，共 {passes} 轮")
        print(f"  合计约 {len(samples) * arguments.strictness * passes} 次")
        return

    from langchain_openai import ChatOpenAI
    from ragas.llms import LangchainLLMWrapper

    model_kwargs: dict = {
        "model": env["RAGAS_JUDGE_MODEL"],
        "api_key": env["RAGAS_JUDGE_API_KEY"],
        "base_url": env["RAGAS_JUDGE_BASE_URL"],
        "max_tokens": arguments.max_tokens,
        "temperature": 0,
        "timeout": 180,
        "max_retries": 2,
    }
    llm = LangchainLLMWrapper(ChatOpenAI(**model_kwargs), bypass_n=True)

    embedding_path = env.get("EMBEDDING_MODEL_PATH")
    if not embedding_path:
        raise SystemExit(".env 缺少 EMBEDDING_MODEL_PATH")
    embeddings = build_embeddings(embedding_path)
    print(f"判分模型：{env['RAGAS_JUDGE_MODEL']}  max_tokens={arguments.max_tokens}  thinking=on")
    print(f"embedding：{embedding_path}（本地、离线）")
    # 必须 bypass_n：DeepSeek 只接受 n=1（实测报 "Invalid n value (currently only n = 1
    # is supported)"），而 AnswerRelevancy 用 n=strictness 一次要 3 个问题。bypass_n 让
    # 包装器改成发 3 个 n=1 的请求，而不是把 n=3 塞进一个请求。既有三个指标从不使用
    # n>1，所以这个开关对它们没有任何影响。
    print(f"bypass_n=True（DeepSeek 不支持 n>1）；strictness={arguments.strictness}\n")

    started = time.perf_counter()
    metric = build_metric(llm, embeddings, arguments.strictness)

    print("--- 主测：用留存的答案判分（第 1 轮）---")
    main_first = asyncio.run(run_pass(
        samples, metric=metric, response_of=lambda s: s["saved_answer"],
        label="主1", delay=arguments.delay,
    ))

    repeat = None
    if not arguments.skip_repeat:
        print("\n--- 稳定性：同一批输入再判一次（第 2 轮）---")
        metric = build_metric(llm, embeddings, arguments.strictness)
        repeat = asyncio.run(run_pass(
            samples, metric=metric, response_of=lambda s: s["saved_answer"],
            label="主2", delay=arguments.delay,
        ))

    print("\n--- 受控对照：把参考 rubric 当答案 ---")
    metric = build_metric(llm, embeddings, arguments.strictness)
    control_gold = asyncio.run(run_pass(
        samples, metric=metric, response_of=lambda s: s["reference"],
        label="对照gold", delay=arguments.delay,
    ))

    print("\n--- 受控对照：把检索到的上下文原文当答案 ---")
    metric = build_metric(llm, embeddings, arguments.strictness)
    control_context = asyncio.run(run_pass(
        samples, metric=metric, response_of=lambda s: s["context_text"],
        label="对照ctx", delay=arguments.delay,
    ))

    # 上界对照：把问题原文当答案。生成的"问题"必然与原问题几乎同义，余弦应当接近 1.0。
    # 它不测质量，只测链路：这里拿不到高分就说明 embedding/余弦这段本身有问题。
    print("\n--- 受控对照：把问题原文当答案（链路上界）---")
    metric = build_metric(llm, embeddings, arguments.strictness)
    control_echo = asyncio.run(run_pass(
        samples, metric=metric, response_of=lambda s: s["user_input"],
        label="对照echo", delay=arguments.delay,
    ))

    elapsed = round(time.perf_counter() - started, 1)

    def scored(records: list[dict]) -> list[float]:
        return [record["score"] for record in records if record.get("score") is not None]

    main_values = scored(main_first)
    zeroed = [record["id"] for record in main_first
              if record.get("zeroed_by_noncommittal")]
    failed = [record["id"] for record in main_first if record.get("score") is None]

    records = {"main": main_first,
               "main_repeat": repeat,
               "control_gold_rubric": control_gold,
               "control_retrieved_context": control_context,
               "control_question_echo": control_echo}

    report = {
        "schema_version": 1,
        "scope": (
            "AnswerRelevancy（RAGAS 的 answer_relevancy）。它只看 user_input 与 response，"
            "不读 retrieved_contexts 与 reference：让判分模型从答案倒推 3 个问题，"
            "再算这 3 个问题与原问题的余弦相似度均值；3 个问题全被判为 noncommittal 时乘 0。"
            "因此它衡量的是「答案的措辞像不像在回答这个问题」，与检索质量无关。"
        ),
        "reuse_of_existing_run": {
            "answers_source": str(arguments.source.relative_to(ROOT)).replace("\\", "/"),
            "regenerated_answers": False,
            "note": (
                "答案沿用既有报告 records[].answer，未重新生成；题集未改动（seal 状态不变），"
                "检索结果未重跑。"
            ),
        },
        "answer_generation": {
            "model": (source_report.get("answer_generation") or {}).get("model"),
            "max_tokens": (source_report.get("answer_generation") or {}).get("max_tokens"),
            "prompt": ANSWER_PROMPT,
            "answered": len(samples),
        },
        "dataset": {
            "path": str(arguments.dataset.relative_to(ROOT)).replace("\\", "/"),
            "questions": len(samples),
        },
        "retrieval_source": {
            "path": str(arguments.retrieval.relative_to(ROOT)).replace("\\", "/"),
            "retriever": arguments.retriever,
            "top_k": arguments.top_k,
        },
        "judge": {
            "model": env["RAGAS_JUDGE_MODEL"],
            "base_url_host": env["RAGAS_JUDGE_BASE_URL"].split("//")[-1].split("/")[0],
            "max_tokens": arguments.max_tokens,
            "temperature": 0,
            "thinking_disabled": False,
            "api_key": "not recorded",
            "same_as_existing_metrics": True,
            # 说清楚：上面那个 temperature=0 对本指标**不生效**。
            "effective_temperature": 0.3,
            "temperature_note": (
                "本指标每题要倒推 strictness(=3) 个问题，而 ragas 的 "
                "RunConfig.get_temperature(n) 在 n>1 时返回 0.3 并覆盖 ChatOpenAI 的 "
                "temperature=0。所以答案相关性这一项天生带采样随机性，"
                "不能像 context_recall / context_precision 那样指望逐题可复现。"
            ),
        },
        "embeddings": {
            "source": "EMBEDDING_MODEL_PATH（项目检索用的同一份本地模型）",
            "model_dir": Path(embedding_path).name,
            "device": "cpu",
            "normalize_embeddings": True,
            "query_instruction_prefix": "none（与 src/wenjin/settings.py 一致）",
            "strictness": arguments.strictness,
            "downloaded_at_runtime": False,
        },
        "summary": {
            "answer_relevancy": describe(main_values),
            "failed": len(failed),
            "failed_ids": failed,
            "zeroed_by_noncommittal": len(zeroed),
            "zeroed_by_noncommittal_ids": zeroed,
        },
        "reliability": {
            "determinism": (
                None if repeat is None else {
                    "same_input_scored_twice": compare(main_first, repeat),
                    "round2_scores": describe(scored(repeat)),
                }
            ),
            "control_gold_rubric_as_answer": {
                "what": "把参考 rubric 原文当答案喂进去；它就是针对该问题的标准答案",
                "expectation": "应当接近 1.0",
                "scores": describe(scored(control_gold)),
                "zeroed_by_noncommittal": sum(
                    1 for record in control_gold if record.get("zeroed_by_noncommittal")),
            },
            "control_retrieved_context_as_answer": {
                "what": "把检索到的上下文原文当答案喂进去",
                "expectation": "包含答案所需内容，但措辞不是说在回答问题，故低于 gold",
                "scores": describe(scored(control_context)),
                "zeroed_by_noncommittal": sum(
                    1 for record in control_context if record.get("zeroed_by_noncommittal")),
            },
            "control_question_echo_as_answer": {
                "what": "把问题原文当答案喂进去（上界对照）",
                "expectation": "倒推出的问题必然与原问题几乎同义，应当接近 1.0；只测链路，不测质量",
                "scores": describe(scored(control_echo)),
                "zeroed_by_noncommittal": sum(
                    1 for record in control_echo if record.get("zeroed_by_noncommittal")),
            },
        },
        "elapsed_seconds": elapsed,
        "verdict": verdict_of(records, repeat),
        "records": records,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    print(f"\n耗时 {elapsed}s")
    for name, values in (
        ("answer_relevancy（留存答案）", main_values),
        ("对照 gold rubric", scored(control_gold)),
        ("对照 检索上下文", scored(control_context)),
        ("对照 问题原文(上界)", scored(control_echo)),
        ("第 2 轮重复", scored(repeat or [])),
    ):
        stats = describe(values)
        if stats["n"]:
            print(f"  {name:28s} n={stats['n']:2d}  "
                  f"{stats['mean']:.4f} ± {stats['std'] if stats['std'] is None else format(stats['std'], '.4f')}"
                  f"  [{stats['min']}, {stats['max']}]")
    if repeat is not None:
        print("  稳定性:", json.dumps(compare(main_first, repeat)["mean_abs_diff"]))
    print(f"报告：{arguments.output}")


if __name__ == "__main__":
    sys.exit(main())
