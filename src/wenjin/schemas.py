"""解析结果契约：先保留页面与结构，后续再决定怎样切分。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Table:
    # PDF 坐标：左上角原点，单位为 point；不能与像素坐标混用。
    bbox: tuple[float, float, float, float]
    # None 表示解析器未给出内容，可能是合并单元格，不自动填充猜测。
    rows: list[list[str | None]]


@dataclass
class ParsedPage:
    source_path: str
    source_sha256: str
    page_number: int  # 对用户统一使用从 1 开始的物理页码。
    parser: str
    text: str
    tables: list[Table] = field(default_factory=list)
    image_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_document(self) -> Any:
        """适配 LlamaIndex；一页 Document 仍不等于最终检索 Node。

        本适配仅承载页级文本。表格结构留在 ParsedPage 中，后续单独序列化；
        不把表格原始嵌套数组当作自然语言，也不悄悄追加重复表格正文。
        """
        from llama_index.core import Document

        return Document(
            id_=f"{self.source_sha256}:{self.parser}:{self.page_number}",
            text=self.text,
            metadata={
                "source_path": self.source_path,
                "source_sha256": self.source_sha256,
                "page_number": self.page_number,
                "parser": self.parser,
            },
            excluded_embed_metadata_keys=["source_sha256", "parser", "source_path"],
            excluded_llm_metadata_keys=["source_sha256", "parser"],
        )
