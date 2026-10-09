"""统一读取本地配置；密钥不提供 repr，也不打印。"""

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8-sig", extra="ignore",
        populate_by_name=True,
    )
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str
    # 远程模式明确通过模型API；默认仍是原来的本地模式。
    model_provider: str = Field(default="local", validation_alias="WENJIN_MODEL_PROVIDER")
    model_api_url: str | None = Field(default=None, validation_alias="WENJIN_MODEL_API_URL")
    model_api_token: SecretStr | None = Field(default=None, validation_alias="WENJIN_MODEL_API_TOKEN")
    model_api_ca_bundle: Path | None = Field(
        default=None, validation_alias="WENJIN_MODEL_API_CA_BUNDLE"
    )
    model_api_timeout_seconds: float = Field(
        default=30.0, validation_alias="WENJIN_MODEL_API_TIMEOUT_SECONDS"
    )
    managed_raw_data_root: Path | None = Field(
        default=None, validation_alias="WENJIN_MANAGED_RAW_DATA_ROOT"
    )
    # 默认值只是占位：真实路径由 .env 给出（见 .env.example），别在这里写死某台机器的布局。
    embedding_model_path: Path = Path("./models/Qwen3-Embedding-0.6B")
    # 可选稳定身份供索引声明和Serving gate比较；未设置时使用模型目录名。
    embedding_model_id: str | None = None
    reranker_model_path: Path = Path("./models/Qwen3-Reranker-0.6B")
    reranker_model_id: str | None = None
    # 在线管理shared检索与MCP共用预算；评测模板仍绑定各自固定控变量。
    search_candidate_k: int = 10
    search_rerank_pool: int = 30
    search_rrf_k: int = 60
    search_max_libraries: int = 5
    search_context_char_budget: int = 8000
    search_reranker_enabled: bool = True
    reranker_max_length: int = 1024
    reranker_batch_size: int = 2

    @model_validator(mode="after")
    def _validate_model_provider(self) -> "Settings":
        self.validate_model_provider()
        return self

    def validate_model_provider(self) -> None:
        if self.model_provider not in {"local", "remote"}:
            raise ValueError("WENJIN_MODEL_PROVIDER必须为local或remote")
        if self.model_api_timeout_seconds <= 0 or self.model_api_timeout_seconds > 300:
            raise ValueError("WENJIN_MODEL_API_TIMEOUT_SECONDS必须在0到300秒之间")
        if self.model_provider == "remote":
            if not self.model_api_url or not self.model_api_url.strip():
                raise ValueError("远程模型模式缺少WENJIN_MODEL_API_URL")
            if self.model_api_token is None or not self.model_api_token.get_secret_value():
                raise ValueError("远程模型模式缺少WENJIN_MODEL_API_TOKEN")
            if not self.embedding_model_id or not self.reranker_model_id:
                raise ValueError("远程模型模式必须显式配置EMBEDDING_MODEL_ID与RERANKER_MODEL_ID")

    @classmethod
    def for_project_root(cls, root: Path, **overrides) -> "Settings":
        """从指定项目根读取配置，避免管理HTTP和MCP误读不同目录的.env。"""
        return cls(_env_file=Path(root).resolve() / ".env", **overrides)

    @property
    def resolved_embedding_model_id(self) -> str:
        """管理HTTP与MCP共用同一套模型ID优先级。"""
        if self.model_provider == "remote":
            self.validate_model_provider()
            return str(self.embedding_model_id)
        return self.embedding_model_id or self.embedding_model_path.name

    @property
    def resolved_reranker_model_id(self) -> str:
        """模型身份仅用于可观察配置；密钥和绝对路径不会进入响应。"""
        if self.model_provider == "remote":
            self.validate_model_provider()
            return str(self.reranker_model_id)
        return self.reranker_model_id or self.reranker_model_path.name

    def resolved_model_path(self, field: str, *, root: Path) -> Path:
        """将embedding/reranker相对路径按同一项目根解析。"""
        if field not in {"embedding_model_path", "reranker_model_path"}:
            raise ValueError("未知模型路径配置")
        path = Path(getattr(self, field))
        return path.resolve() if path.is_absolute() else (Path(root).resolve() / path).resolve()

    def embedding(self, *, root: Path | None = None):
        """返回本地文档 Embedding 适配器；模型只在调用时加载。"""
        if self.model_provider != "local":
            raise RuntimeError("远程模型模式请通过模型provider构造Embedding适配器")
        import torch
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding

        model_path = (
            self.resolved_model_path("embedding_model_path", root=root)
            if root is not None else Path(self.embedding_model_path)
        )
        return HuggingFaceEmbedding(
            model_name=str(model_path),
            device="cuda" if torch.cuda.is_available() else "cpu",
            embed_batch_size=16,
            normalize=True,
            trust_remote_code=True,
        )
