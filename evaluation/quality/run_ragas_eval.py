"""用 RAGAS 的**检索侧**指标复核冻结 test：LLMContextRecall + LLMContextPrecisionWithReference。

为什么只跑这两个指标
--------------------
RAGAS 的招牌指标 Faithfulness / AnswerRelevancy 都需要**生成好的答案**，而本项目
只返回证据、不生成答案。硬跑就得先加一个生成步骤，那测的就变成"生成模型写得好不好"，
与检索质量混在一起。所以这里只用不需要生成答案、只要 `reference` 的两个检索侧指标。

为什么值得跑
------------
本项目既有的 span_recall 是**词法/位置**判定："那几条登记的证据行进没进 top-k"。
RAGAS 是**语义**判定："检索到的内容够不够支撑参考答案"。两者对照才能看出偏在哪：

- RAGAS 明显更高 → 漏掉的证据其实被内容相近的别的 chunk 覆盖了，本项目的指标偏严；
- RAGAS 明显更低 → 证据行虽然进了 top-k，但上下文不足以支撑答案，本项目的指标偏松。

题集里写着 `evidence_policy: listed spans jointly support the rubric; not exhaustive`
——登记的证据是充分条件而非唯一解，所以"够不够支撑答案"这种问法其实更贴近意图。

输入不是重新检索
----------------
直接复用 `artifacts/frozen-test-001/test-retrieval-eval.json` 里已记录的 rerank top-k
chunk ID，再从 BM25 库按 ID 取回正文。因此本脚本**不需要 Qdrant、不需要本地模型**，
只跑在 `evaluation/ragas/` 这个独立环境里。用哪一路检索结果由 `--retriever` 决定。

生成答案与判分是**两个模型、两套配置**
--------------------------------------
- **生成答案**：`--with-answers` 时用 `ANSWER_*`（未填则回落到 `QUERY_REWRITE_*`，
  即讯飞 `xopqwen35397b`）。本项目服务端**不生成答案**（MCP 返回
  `answer_status: not_generated`），这一步只存在于评测里，用来把
  `Faithfulness` / `FactualCorrectness` 跑起来。它在评测内部加一层生成，
  不改变服务"只交付证据"的契约。
- **判分**：始终用 `RAGAS_JUDGE_*`（DeepSeek `deepseek-flash`）。

注意判分模型若是推理模型，`max_tokens` 必须给足：给少了会**静默返回空内容**，
不报错但判分全废。实测 `deepseek-flash` 默认每次烧约 250 token 在思考上。

不跑的指标
----------
- `AnswerRelevancy` / `AnswerCorrectness`：需要 embedding 模型，`evaluation/ragas/`
  环境里没有（未装 `sentence_transformers`）。
- `FactualCorrectness`：只需 LLM，但**实测判分不可信**。同一份数据里答案=参考得 1.0、
  答案正确但比参考长得 0.0、把两者对调又变回 1.0——结果严重依赖方向。
  判分链路本身已用受控输入验证过（完全相同→1.0、部分重叠→0.4、中英交叉→0.67），
  所以不是连通性问题。不可信的指标不纳入结论。

运行：
    uv run --project evaluation/ragas python evaluation/quality/run_ragas_eval.py --dry-run
    uv run --project evaluation/ragas python evaluation/quality/run_ragas_eval.py
"""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

os.environ["RAGAS_DO_NOT_TRACK"] = "true"

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RETRIEVAL = ROOT / "artifacts/frozen-test-001/test-retrieval-eval.json"
DEFAULT_DATASET = ROOT / "evaluation/datasets/rag-test-001.jsonl"
DEFAULT_ROUTE = ROOT / "configs/indexes/multicorpus-candidates-002.json"
DEFAULT_OUTPUT = ROOT / "artifacts/frozen-test-001/ragas-retrieval-eval.json"

# 生成答案用的提示词。它是评测口径的一部分，会写进报告——换提示词等于换口径。
ANSWER_PROMPT = (
    "根据下面提供的资料回答问题。\n"
    "要求：只使用资料中的信息，不要补充资料之外的内容；\n"
    "如果资料不足以回答问题，就明确说明资料不足，不要猜测。\n\n"
    "资料：\n{context}\n\n"
    "问题：{question}\n"
    "回答："
)


def load_env(path: Path) -> dict[str, str]:
    """读 .env；密钥不打印、不写进报告。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def normalize_library(name: str) -> str:
    """与 `wenjin.retrieval.router.normalize_library` 同一规则。

    这里内联而不是导入：本脚本跑在 `evaluation/ragas/` 独立环境，没有安装主项目，
    而为了一个两行纯函数去 import router 会把 qdrant_client 等依赖一并拖进来。
    两边若将来不一致，`--dry-run` 会因找不到语料而暴露。
    """
    return re.sub(r"[\s_]+", "-", name.strip().lower())


def bm25_paths(route_path: Path) -> dict[str, Path]:
    """library 归一化名 → BM25 路径。"""
    route = json.loads(route_path.read_text(encoding="utf-8-sig"))
    return {
        normalize_library(item["library"]): ROOT / item["bm25_path"]
        for item in route["candidates"]
    }


def library_of(chunk_id: str) -> str | None:
    """从 chunk ID 里取出语料库名。

    ID 形如 `3.13.1:raw_data/gdal/3.13.1/gdaldoc/x.html:blocks-split:0000`，
    `raw_data/` 后面那一段就是语料库。直接取段而不是拿库名去子串匹配：
    子串匹配在库名互为前缀时会认错（`ogc-wms` / `ogc-3dtiles` 只是碰巧不冲突）。
    """
    marker = "raw_data/"
    index = chunk_id.find(marker)
    if index < 0:
        return None
    rest = chunk_id[index + len(marker):]
    segment = rest.split("/", 1)[0]
    return normalize_library(segment) if segment else None


def chunk_texts(paths: dict[str, Path], chunk_ids: list[str]) -> tuple[dict[str, str], list[str]]:
    """按 chunk ID 取正文；按语料分组查询，避免对每个 ID 各开一次连接。

    返回 (命中的正文, 无法定位的 chunk ID)。
    """
    wanted: dict[Path, set[str]] = {}
    unresolved: list[str] = []
    for chunk_id in chunk_ids:
        library = library_of(chunk_id)
        path = paths.get(library) if library else None
        if path is None:
            unresolved.append(chunk_id)
            continue
        wanted.setdefault(path, set()).add(chunk_id)

    found: dict[str, str] = {}
    for path, ids in wanted.items():
        if not path.is_file():
            unresolved.extend(sorted(ids))
            continue
        with closing(sqlite3.connect(path)) as database:
            for chunk_id in ids:
                row = database.execute(
                    "SELECT raw_text FROM chunks WHERE chunk_id = ?", (chunk_id,)
                ).fetchone()
                if row and row[0]:
                    found[chunk_id] = row[0]
                else:
                    unresolved.append(chunk_id)
    return found, unresolved


def build_samples(
    dataset_path: Path, retrieval_path: Path, paths: dict[str, Path], *, retriever: str, top_k: int
) -> tuple[list[dict], list[dict]]:
    """把题集与已记录的检索结果拼成 RAGAS 样本；返回 (样本, 未取到正文的条目)。"""
    rows = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    retrieval = json.loads(retrieval_path.read_text(encoding="utf-8"))
    top_ids = {item["id"]: item["top_ids"] for item in retrieval["details"]}

    samples, missing = [], []
    for row in rows:
        ids = top_ids.get(row["id"], {}).get(retriever, [])[:top_k]
        texts, unresolved = chunk_texts(paths, ids)
        ordered = [texts[i] for i in ids if i in texts]
        if not ordered:
            missing.append({"id": row["id"], "reason": "no_context_text",
                            "requested": len(ids), "unresolved": unresolved[:3]})
            continue
        samples.append({
            "id": row["id"],
            "user_input": row["query"],
            "retrieved_contexts": ordered,
            "reference": row["answer_rubric"],
            "library": row["library"],
            "expected_status": row.get("expected_status"),
            "requested_contexts": len(ids),
            "resolved_contexts": len(ordered),
        })
    return samples, missing


def answer_model_config(env: dict[str, str]) -> dict[str, str]:
    """生成答案用的模型配置：优先 ANSWER_*，否则回落到 QUERY_REWRITE_*。

    回落而不是复制一份值进 .env：两者当前指向同一个讯飞模型，但角色不同
    （一个是生产查询改写、一个是评测答题），所以留出独立覆盖的口子。
    """
    return {
        "base_url": env.get("ANSWER_BASE_URL") or env.get("QUERY_REWRITE_BASE_URL", ""),
        "api_key": env.get("ANSWER_API_KEY") or env.get("QUERY_REWRITE_API_KEY", ""),
        "model": env.get("ANSWER_MODEL") or env.get("QUERY_REWRITE_MODEL", ""),
    }


def generate_answers(
    samples: list[dict], config: dict[str, str], *, max_tokens: int, timeout: float
) -> list[dict]:
    """用生成模型对每题作答；返回失败记录（成功时把答案写进 sample["answer"]）。"""
    import httpx

    failures = []
    url = f"{config['base_url'].rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {config['api_key']}",
               "Content-Type": "application/json"}
    for index, sample in enumerate(samples, start=1):
        prompt = ANSWER_PROMPT.format(
            context="\n\n".join(sample["retrieved_contexts"]),
            question=sample["user_input"],
        )
        try:
            response = httpx.post(
                url, headers=headers,
                json={"model": config["model"], "temperature": 0, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout, trust_env=False,
            )
            response.raise_for_status()
            content = (response.json()["choices"][0]["message"].get("content") or "").strip()
        except Exception as error:  # noqa: BLE001 - 单题失败不该中断整轮，但要如实记录
            failures.append({"id": sample["id"],
                             "error": f"{type(error).__name__}: {str(error)[:160]}"})
            sample["answer"] = None
            continue
        if not content:
            # 推理模型 token 给少了会走到这里：HTTP 200、content 为空。
            failures.append({"id": sample["id"], "error": "empty_content"})
            sample["answer"] = None
        else:
            sample["answer"] = content
        print(f"  [答 {index:2d}/{len(samples)}] {sample['id']:14s} "
              f"{' '.join(content.split())[:70]}", flush=True)
    return failures


async def score(samples: list[dict], *, llm, metrics: dict, delay: float) -> list[dict]:
    from ragas import SingleTurnSample

    records = []
    for index, sample in enumerate(samples, start=1):
        # 全部字段都给上，由各指标自己取用需要的那几个；
        # `response` 只有生成过答案时才存在，否则 Faithfulness 一类会判失败。
        fields = {
            "user_input": sample["user_input"],
            "retrieved_contexts": sample["retrieved_contexts"],
            "reference": sample["reference"],
        }
        if sample.get("answer"):
            fields["response"] = sample["answer"]
        ragas_sample = SingleTurnSample(**fields)
        record = {k: sample[k] for k in
                  ("id", "library", "expected_status", "requested_contexts",
                   "resolved_contexts")}
        if sample.get("answer"):
            # 把生成的答案留在报告里：分数之外，人还得能读到模型到底答了什么。
            record["answer"] = sample["answer"]
        for name, metric in metrics.items():
            started = time.perf_counter()
            try:
                value = await metric.single_turn_ascore(ragas_sample)
                record[name] = None if value is None else round(float(value), 4)
            except Exception as error:  # noqa: BLE001 - 单题失败不该中断整轮，但要如实记录
                record[name] = None
                record[f"{name}_error"] = f"{type(error).__name__}: {str(error)[:160]}"
            record[f"{name}_seconds"] = round(time.perf_counter() - started, 2)
        records.append(record)
        done = [record.get(n) for n in metrics]
        print(f"  [{index:2d}/{len(samples)}] {sample['id']:14s} "
              + "  ".join(f"{n}={v}" for n, v in zip(metrics, done)), flush=True)
        if delay:
            await asyncio.sleep(delay)
    return records


def summarize(records: list[dict], metric_names: list[str]) -> dict:
    summary = {}
    for name in metric_names:
        values = [r[name] for r in records if r.get(name) is not None]
        failed = [r["id"] for r in records if r.get(name) is None]
        summary[name] = {
            "scored": len(values),
            "failed": len(failed),
            "failed_ids": failed,
            "mean": round(sum(values) / len(values), 4) if values else None,
            "min": round(min(values), 4) if values else None,
            "max": round(max(values), 4) if values else None,
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="用 RAGAS 检索侧指标复核冻结 test")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--retrieval", type=Path, default=DEFAULT_RETRIEVAL)
    parser.add_argument("--route", type=Path, default=DEFAULT_ROUTE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--retriever", default="rerank",
                        choices=["dense", "bm25", "rrf", "rerank"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="推理模型给少了会静默返回空内容")
    parser.add_argument("--disable-thinking", action="store_true",
                        help="关掉推理（实测快一倍、不烧推理 token；判分质量需自行确认）")
    parser.add_argument("--delay", type=float, default=0.0, help="每题之间的间隔秒数")
    parser.add_argument("--limit", type=int, help="只跑前 N 题，用于先验证链路")
    parser.add_argument("--with-answers", action="store_true",
                        help="先用 ANSWER_* 模型生成答案，再跑 Faithfulness（幻觉检查）")
    parser.add_argument("--answer-max-tokens", type=int, default=1024,
                        help="生成答案的 token 上限（实测讯飞模型 64 已够短答案）")
    parser.add_argument("--dry-run", action="store_true", help="只组装样本，不调用模型")
    arguments = parser.parse_args()

    env = load_env(ROOT / ".env")
    missing_config = [k for k in
                      ("RAGAS_JUDGE_BASE_URL", "RAGAS_JUDGE_API_KEY", "RAGAS_JUDGE_MODEL")
                      if not env.get(k)]
    if missing_config and not arguments.dry_run:
        raise SystemExit(f".env 缺少判分配置：{missing_config}")

    answer_cfg = answer_model_config(env)
    paths = bm25_paths(arguments.route)
    samples, missing = build_samples(
        arguments.dataset, arguments.retrieval, paths,
        retriever=arguments.retriever, top_k=arguments.top_k,
    )
    if arguments.limit:
        samples = samples[: arguments.limit]
    print(f"样本 {len(samples)} 题；未取到正文 {len(missing)} 题；"
          f"检索器={arguments.retriever} top_k={arguments.top_k}")
    if missing:
        print("  未取到正文:", [m["id"] for m in missing])
    if not samples:
        raise SystemExit("没有可用样本")

    if arguments.dry_run:
        first = samples[0]
        print("\n--- 样本预览（第 1 题）---")
        print("问题:", first["user_input"][:100])
        print("参考答案:", first["reference"][:100])
        print(f"上下文 {len(first['retrieved_contexts'])} 段，首段前 120 字:")
        print("  ", " ".join(first["retrieved_contexts"][0].split())[:120])
        print("\n--- 会发出的调用次数 ---")
        print(f"  LLMContextRecall:                  {len(samples)} 次")
        print(f"  LLMContextPrecisionWithReference:  {len(samples)} x 最多 "
              f"{arguments.top_k} 次 = 最多 {len(samples) * arguments.top_k} 次")
        total = len(samples) * (1 + arguments.top_k)
        if arguments.with_answers:
            print(f"  生成答案（{answer_cfg['model']}）:   {len(samples)} 次")
            print(f"  Faithfulness:                      {len(samples)} 次")
            total += len(samples) * 2
        print(f"  合计约 {total} 次")
        return

    if arguments.with_answers:
        absent = [key for key, value in answer_cfg.items() if not value]
        if absent:
            raise SystemExit(
                f"生成答案的配置缺失：{absent}（.env 里填 ANSWER_* 或 QUERY_REWRITE_*）"
            )
        print(f"生成答案：{answer_cfg['model']}  max_tokens={arguments.answer_max_tokens}\n")
        generation_failures = generate_answers(
            samples, answer_cfg, max_tokens=arguments.answer_max_tokens, timeout=180
        )
        answered = sum(1 for item in samples if item.get("answer"))
        print(f"\n生成完成：{answered}/{len(samples)} 题有答案；失败 {len(generation_failures)} 题")
        if generation_failures:
            print("  失败:", [item["id"] for item in generation_failures])
        missing = missing + generation_failures

    from langchain_openai import ChatOpenAI
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
    )

    model_kwargs: dict = {
        "model": env["RAGAS_JUDGE_MODEL"],
        "api_key": env["RAGAS_JUDGE_API_KEY"],
        "base_url": env["RAGAS_JUDGE_BASE_URL"],
        "max_tokens": arguments.max_tokens,
        "temperature": 0,
        "timeout": 180,
        "max_retries": 2,
    }
    if arguments.disable_thinking:
        model_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    llm = LangchainLLMWrapper(ChatOpenAI(**model_kwargs))

    metrics = {
        "context_recall": LLMContextRecall(llm=llm),
        "context_precision": LLMContextPrecisionWithReference(llm=llm),
    }
    if arguments.with_answers:
        # 生成答案带来的新信息：答案有没有忠于检索到的上下文（幻觉检查）。
        metrics["faithfulness"] = Faithfulness(llm=llm)
    print(f"判分模型：{env['RAGAS_JUDGE_MODEL']}  max_tokens={arguments.max_tokens} "
          f"thinking={'off' if arguments.disable_thinking else 'on'}\n")

    started = time.perf_counter()
    records = asyncio.run(
        score(samples, llm=llm, metrics=metrics, delay=arguments.delay)
    )
    elapsed = round(time.perf_counter() - started, 1)

    report = {
        "schema_version": 1,
        "scope": (
            "RAGAS 检索侧指标（不生成答案）。context_recall / context_precision 均为 "
            "LLM 语义判定，与项目既有的 span_recall（词法/位置判定）互为对照，"
            "两者不是同一个量，不可互相替代。"
        ),
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
            "thinking_disabled": arguments.disable_thinking,
            "api_key": "not recorded",
        },
        "answer_generation": ({
            "model": answer_cfg["model"],
            "max_tokens": arguments.answer_max_tokens,
            "prompt": ANSWER_PROMPT,
            "answered": sum(1 for item in samples if item.get("answer")),
            "note": (
                "服务端不生成答案（MCP 返回 answer_status=not_generated）；"
                "这一步只存在于评测里，用来把 Faithfulness / FactualCorrectness 跑起来。"
                "换提示词等于换口径。"
            ),
        } if arguments.with_answers else None),
        "judge_reliability": {
            "context_recall": (
                "可用。受控验证：expected_status=insufficient 的 040 得 0.0、"
                "partially_supported 的 039 得 0.75，与标注一致，说明判分在区分。"
                "两轮重跑的逐题波动：3/40 题不同，题均绝对差 0.0175。"
            ),
            "context_precision": "可用。同上。",
            "faithfulness": (
                "**不可用，不纳入结论**。受控验证：把**检索到的上下文原文本身**当作"
                "答案喂进去，应得 1.0，实测只有 0.857；同一份输入连续两次判分得 0.4 与 "
                "0.667（波动 0.27）。判分链路没有故障（context 两项指标表现正常），"
                "是这个指标配这个判分模型时噪声过大。"
            ),
        },
        "metrics_not_run": {
            "answer_relevancy": "需要 embedding 模型；evaluation/ragas/ 环境里未安装",
            "answer_correctness": "同上",
            "factual_correctness": (
                "实测判分不可信，不纳入结论。同一份数据里：答案=参考 → 1.0；"
                "答案正确但比参考长 → 0.0；把两者对调 → 又变回 1.0。"
                "结果严重依赖方向，无法作为答案正确性的依据。"
                "判分链路本身已用受控输入验证过（完全相同→1.0、部分重叠→0.4、"
                "中英交叉→0.67），因此不是 API 或连通性问题。"
            ),
            **({} if arguments.with_answers else {
                "faithfulness": "需要生成答案；本次未加 --with-answers",
            }),
        },
        "samples_without_context": missing,
        "summary": summarize(records, list(metrics)),
        "elapsed_seconds": elapsed,
        "records": records,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"\n耗时 {elapsed}s")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"报告：{arguments.output}")


if __name__ == "__main__":
    sys.exit(main())
