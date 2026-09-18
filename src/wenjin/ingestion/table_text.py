"""把已经恢复的表格记录序列化成带上下文的 RAG 文本。

这里不负责判断表格是否解析正确；输入应先经过 bbox/人工质量检查。
"""


def record_to_text(record: dict, *, title: str, period: str) -> str:
    """把一条结构化记录变成可读文本，并重复必要的表头上下文。"""
    # 不只返回数值：行名、列名和表格背景必须随数值一起进入检索文本。
    return "\n".join(
        [
            f"表格：{title}",
            f"时期：{period}",
            f"State / Territory：{record['state']}",
            f"Permit：{record['permit']}",
            f"Totals：{record['totals']}",
        ]
    )


def records_to_chunks(records: list[dict], *, title: str, period: str) -> list[dict]:
    """每条业务记录生成一个 chunk，同时保留原始页面来源和 bbox。"""
    chunks = []
    for index, record in enumerate(records):
        chunks.append(
            {
                "chunk_id": f"table-row-{index + 1:03d}",
                "text": record_to_text(record, title=title, period=period),
                "metadata": {
                    "source_file": record["source"]["file"],
                    "page_number": record["source"]["page_number"],
                    "bbox": record["source"]["bbox"],
                    "table_title": title,
                    "period": period,
                    "row_key": record["state"],
                    "parser_status": "coordinate_recovered_demo",
                },
            }
        )
    return chunks
