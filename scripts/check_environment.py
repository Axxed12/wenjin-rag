"""验证依赖、GPU、数据库和现有 MinerU 服务；--models 额外执行离线模型推理。

uv run python scripts/check_environment.py
uv run python scripts/check_environment.py --models
"""

import argparse
import gc
import importlib.metadata
import io
import json
import os
import sqlite3
import uuid
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    # 环境变量优先于 .env；不打印 API key。
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8-sig", extra="ignore"
    )
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str
    mineru_url: str = "http://127.0.0.1:8000"
    embedding_model_path: Path
    reranker_model_path: Path


def check_basics(settings: Settings) -> dict:
    import httpx
    import pdfplumber
    import pymupdf
    import torch
    from bs4 import BeautifulSoup
    from fastmcp import FastMCP
    from llama_index.core import Document
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    from llama_index.vector_stores.qdrant import QdrantVectorStore
    from qdrant_client import QdrantClient, models

    packages = [
        "torch",
        "transformers",
        "sentence-transformers",
        "llama-index-core",
        "llama-index-embeddings-huggingface",
        "llama-index-vector-stores-qdrant",
        "qdrant-client",
        "pymupdf",
        "pdfplumber",
        "fastmcp",
        "pytest",
        "ruff",
    ]
    result = {"versions": {p: importlib.metadata.version(p) for p in packages}}
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable: check the installed torch build and driver")
    # 真正在 GPU 上做运算，不能只看驱动能否识别显卡。
    assert torch.ones(4, device="cuda").sum().item() == 4
    result["gpu"] = {
        "name": torch.cuda.get_device_name(0),
        "torch_cuda": torch.version.cuda,
    }
    assert Document(text="environment smoke check").text
    assert HuggingFaceEmbedding and QdrantVectorStore and FastMCP("environment-check")
    assert BeautifulSoup("<p>hello</p>", "lxml").get_text() == "hello"

    # 生成内存 PDF 后由两种解析器读取，只验证工具可用，不是解析质量评测。
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "RAG environment check")
        pdf_bytes = pdf.tobytes()
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        assert "RAG environment check" in pdf[0].get_text()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        assert "RAG environment check" in pdf.pages[0].extract_text()
    result["pdf_parsers"] = "PASS"

    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE VIRTUAL TABLE docs USING fts5(text)")
        db.execute("INSERT INTO docs VALUES (?)", ("osg Geode addDrawable",))
        assert db.execute(
            "SELECT text FROM docs WHERE docs MATCH ? ORDER BY bm25(docs)",
            ("addDrawable",),
        ).fetchone()
    result["sqlite_fts5"] = "PASS"

    # 使用唯一临时 collection，验证新增、过滤检索、删除，绝不触碰用户集合。
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    name = "env_check_" + uuid.uuid4().hex
    created = False
    try:
        client.create_collection(
            name,
            vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        created = True
        client.upsert(
            name,
            points=[models.PointStruct(id=1, vector=[1.0, 0.0, 0.0], payload={"library": "osg"})],
            wait=True,
        )
        hits = client.query_points(
            name,
            query=[1.0, 0.0, 0.0],
            query_filter=models.Filter(
                must=[models.FieldCondition(key="library", match=models.MatchValue(value="osg"))]
            ),
        ).points
        assert len(hits) == 1 and hits[0].id == 1
        result["qdrant"] = "PASS: write and filtered query"
    finally:
        if created:
            client.delete_collection(name)
        client.close()

    with httpx.Client(timeout=10, trust_env=False) as http:
        response = http.get(settings.mineru_url + "/health")
        response.raise_for_status()
        health = response.json()
        assert health["status"] == "healthy"
    result["mineru"] = {
        "status": health["status"],
        "version": health.get("version"),
        "scope": "health only; no OCR job submitted",
    }
    return result


def check_models(settings: Settings) -> dict:
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    from transformers import AutoModelForCausalLM, AutoTokenizer

    # 显式使用本地路径和 local_files_only，SDPA 避免额外安装 flash-attn。
    embedding = SentenceTransformer(
        str(settings.embedding_model_path),
        device="cuda",
        local_files_only=True,
        model_kwargs={"torch_dtype": torch.float16, "attn_implementation": "sdpa"},
    )
    embedding.max_seq_length = 512
    vectors = embedding.encode(
        ["What is the capital of China?"],
        prompt_name="query",
        normalize_embeddings=True,
    )
    docs = embedding.encode(
        ["The capital of China is Beijing.", "A triangle has three sides."],
        normalize_embeddings=True,
    )
    assert vectors.shape == (1, 1024) and docs.shape == (2, 1024)
    assert np.isfinite(vectors).all() and np.isfinite(docs).all()
    assert np.allclose(np.linalg.norm(docs, axis=1), 1.0, atol=1e-3)
    result = {"embedding": {"dimension": 1024, "scores": (vectors @ docs.T).tolist()}}
    del embedding
    gc.collect()
    torch.cuda.empty_cache()

    # 用官方 Transformers 格式做一次重排冒烟检查；不是完整业务 reranker。
    tokenizer = AutoTokenizer.from_pretrained(
        settings.reranker_model_path, local_files_only=True, padding_side="left"
    )
    model = (
        AutoModelForCausalLM.from_pretrained(
            settings.reranker_model_path,
            local_files_only=True,
            torch_dtype=torch.float16,
            attn_implementation="sdpa",
        )
        .to("cuda")
        .eval()
    )
    prefix = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
    suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    texts = [
        prefix
        + "<Instruct>: Given a web search query, retrieve relevant passages that answer the query\n<Query>: What is the capital of China?\n<Document>: "
        + doc
        + suffix
        for doc in ["The capital of China is Beijing.", "A triangle has three sides."]
    ]
    # 此处输入固定且很短，不做截断；业务实现必须给前后缀预留长度。
    inputs = tokenizer(texts, padding=True, return_tensors="pt").to("cuda")
    with torch.inference_mode():
        logits = model(**inputs, use_cache=False).logits[:, -1, :]
        scores = (
            logits[
                :,
                [
                    tokenizer.convert_tokens_to_ids("no"),
                    tokenizer.convert_tokens_to_ids("yes"),
                ],
            ]
            .float()
            .softmax(dim=-1)[:, 1]
            .cpu()
            .tolist()
        )
    assert all(np.isfinite(scores)) and scores[0] > scores[1]
    result["reranker"] = {
        "scores": scores,
        "scope": "two-item smoke check, not quality evaluation",
    }
    del model, inputs, logits
    gc.collect()
    torch.cuda.empty_cache()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", action="store_true")
    args = parser.parse_args()
    # 模型检查中禁止 Hub 下载；不代表已完成整机断网验收。
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    settings = Settings()
    report = check_basics(settings)
    if args.models:
        report["models"] = check_models(settings)
    report["status"] = "PASS"
    output = ROOT / "artifacts" / "environment-check.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
