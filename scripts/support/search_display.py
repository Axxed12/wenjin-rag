"""共享检索摘要格式化逻辑；dense 与 BM25 命令均复用此函数。"""

def format_human_result(query: str, strategy: str, results: list[dict]) -> str:
    """把机器结果压缩成适合人先阅读的检索摘要。"""
    lines = [
        f"查询：{query}",
        f"切分策略：{strategy}",
        f"召回结果：{len(results)} 条",
        "说明：score 是向量相似度排序分数，不是答案正确率。",
    ]
    for index, result in enumerate(results, start=1):
        text = " ".join(str(result.get("text", "")).split())
        preview = text[:360] + ("..." if len(text) > 360 else "")
        lines.extend(
            [
                "",
                f"[{index}] score={result['score']:.5f}",
                f"来源：{result.get('source_path')}:{result.get('start_line')}-{result.get('end_line')}",
                f"版本：{result.get('version')}  策略：{result.get('strategy')}",
                f"chunk_id：{result.get('chunk_id')}",
                f"正文预览：{preview}",
            ]
        )
    return "\n".join(lines)
