"""语料身份的唯一定义与校验入口。

一个语料的身份可能出现在三个地方：磁盘目录名、manifest里的`corpus_id`、索引目标配置的
显式字段。三者**必须一致**，不一致就拒绝——因为授权、取件和索引切换都以这个字符串为准，
静默挑一个"纠正"另一个会让某个语料的内容在另一个语料的授权下被读走。

本模块不依赖任何其他内部模块，索引注册表、语料目录和原文取件共用同一份判定。
"""

import re
from dataclasses import dataclass

# 只允许字母数字和 . _ -，且不以符号开头。这样它就同时是安全的目录名和collection名前缀。
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CorpusIdentityError(ValueError):
    """语料身份缺失、格式非法，或两个来源互相冲突。"""


@dataclass(frozen=True)
class CorpusIdentity:
    """语料身份及其来源，便于在日志和错误信息里说清依据。"""

    corpus_id: str
    source: str  # explicit / manifest / directory

    def __str__(self) -> str:
        return f"{self.corpus_id}（来源：{self.source}）"


def validate_identifier(value: str, *, field: str) -> str:
    """library/version/corpus_id/目录名只允许安全字符。

    拒绝绝对路径、路径分隔符、`..`和平台特殊路径，避免它们进入磁盘路径或collection名。
    """
    text = (value or "").strip()
    if not IDENTIFIER_PATTERN.fullmatch(text):
        raise CorpusIdentityError(
            f"{field}只能由字母数字和._-组成且不以符号开头，实际为：{value!r}"
        )
    return text


def resolve_identity(
    *,
    directory: str,
    declared: str | None = None,
    explicit: str | None = None,
    context: str = "",
) -> CorpusIdentity:
    """由目录名、manifest声明和（可选的）显式字段确定语料身份。

    - `explicit`：索引目标配置里的显式字段，新配置有，旧配置为None；
    - `declared`：manifest里的`corpus_id`；
    - `directory`：manifest所在目录名，是历史约定，也是最后一道依据。

    三者只要出现两个不同的值就拒绝，绝不自动挑一个"纠正"另一个。
    全部一致时按权威度报告来源：显式字段 > manifest声明 > 目录名。
    """
    candidates: list[tuple[str, str]] = []
    # 空白（含空串）统一按"没有声明"处理，与"旧配置缺字段"是同一条兼容路径；
    # 非空但格式非法仍然拒绝。内容Hash那边相反——空白必须拒绝，因为完整性没有回退项。
    if explicit and explicit.strip():
        candidates.append(("explicit", validate_identifier(explicit, field="corpus_id")))
    if declared and declared.strip():
        candidates.append(("manifest", validate_identifier(declared, field="corpus_id")))
    candidates.append(("directory", validate_identifier(directory, field="语料目录名")))
    distinct = {value for _, value in candidates}
    if len(distinct) > 1:
        detail = "、".join(f"{source}={value}" for source, value in candidates)
        raise CorpusIdentityError(f"语料身份冲突{context}：{detail}")
    source, value = candidates[0]
    return CorpusIdentity(value, source)
