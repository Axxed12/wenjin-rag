"""可解释、可关闭的领域查询扩展实验。

词表由当前开发题和公开源码诊断得到，因此属于开发集调优，必须在冻结测试集上复验。
"""

import re

DOMAIN_ALIASES = {
    "多个子节点": ["Group", "addChild"],
    "相对父节点": ["RELATIVE_RF", "setReferenceFrame"],
    "绝对坐标系": ["ABSOLUTE_RF", "setReferenceFrame"],
    "二维纹理": ["Texture2D"],
    "加载图片": ["readRefImageFile"],
    "图片加载": ["readRefImageFile"],
    "纹理单元": ["setTextureAttributeAndModes"],
}


def expand_query(query: str) -> tuple[str, list[str]]:
    """命中明确短语时追加英文术语；原问题保持不变。"""
    additions = []
    seen = set()
    for phrase, aliases in DOMAIN_ALIASES.items():
        if phrase in query:
            for alias in aliases:
                if alias.lower() not in seen and alias.lower() not in query.lower():
                    seen.add(alias.lower())
                    additions.append(alias)
    expanded = query if not additions else f"{query} {' '.join(additions)}"
    return expanded, additions


def code_identifier_terms(terms: list[str]) -> list[str]:
    """从普通词项中选出更像代码标识符的词，用于头文件支路。"""
    return [
        term
        for term in terms
        if "_" in term or re.search(r"[a-z][A-Z]", term)
    ]


def source_routes(query: str) -> list[str]:
    """从明确意图生成来源前缀；不猜测具体答案文件。"""
    routes = []
    if any(phrase in query for phrase in ("示例", "源码片段", "用法代码")):
        routes.append("examples/%")
    if any(phrase in query for phrase in ("接口", "声明", "API")):
        routes.append("include/%")
    return routes
