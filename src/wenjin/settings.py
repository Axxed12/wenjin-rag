"""统一读取本地配置；密钥不提供 repr，也不打印。"""

from pathlib import Path

import torch
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8-sig", extra="ignore")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str
    embedding_model_path: Path = Path("E:/Models/Qwen3-Embedding-0.6B")
    reranker_model_path: Path = Path("E:/Models/Qwen3-Reranker-0.6B")

    def embedding(self) -> HuggingFaceEmbedding:
        """返回本地文档 Embedding 适配器；模型只在调用时加载。"""
        return HuggingFaceEmbedding(
            model_name=str(self.embedding_model_path),
            device="cuda" if torch.cuda.is_available() else "cpu",
            embed_batch_size=16,
            normalize=True,
            trust_remote_code=True,
        )
