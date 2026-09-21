"""中文分词：让 FTS5 的 unicode61 分词器看到空格分隔的中文词。

unicode61 把一整段连续中文当成**一个**词元：`本接口用于查询用户信息` 只索引出这一个
词元，于是查询 `接口` 命中 0 条，纯中文查询在 BM25 上等于没有召回。这里用 jieba 在
**写入和查询两侧做同一次切分**，把中文段换成空格分隔的词，其余字节（ASCII 标识符、
标点、空白）原样保留，因此纯英文语料的词元集合逐字节不变。

**索引与查询必须使用同一版 jieba**：两边切分一旦不一致，索引里的词元和查询词元就对
不上，中文检索会静默返回空结果（不报错）。换 jieba 版本必须重建 BM25 索引。

jieba 是重依赖（import + 建词典约 0.5 s），因此**惰性加载**：任何模块的顶层 import 都
不会把它带进来。服务应在启动时调用 `prewarm()`，不要让首个请求承担这次初始化。
"""

import re
from threading import Lock

CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")

_lock = Lock()
_jieba = None


def _tokenizer():
    """返回已初始化的 jieba 模块；首次调用在锁内完成建词典。

    jieba 的首次 `cut` 才建词典且自身没有锁，服务用线程池跑工具，两个请求同时首次
    调用会重复建词典甚至读到半成品。这里显式 `initialize()` 并加锁，把风险收敛到
    一次性初始化。
    """
    global _jieba
    module = _jieba
    if module is None:
        with _lock:
            if _jieba is None:
                import jieba

                jieba.initialize()
                _jieba = jieba
            module = _jieba
    return module


def prewarm() -> None:
    """显式预热分词器，供服务启动时调用，避免首个请求吃掉建词典的耗时。"""
    _segment_run("预热")


def _segment_run(run: str) -> str:
    """切分一段连续 CJK，返回空格分隔的词；切不出词时原样返回。"""
    tokens = [token for token in _tokenizer().lcut(run) if token]
    return " ".join(tokens) if tokens else run


def segment_cjk(text: str) -> str:
    """把每个 CJK 连续段替换为切分后以空格连接的形式，其余字节原样保留。"""
    # 无 CJK 时直接返回：纯英文语料走不到 jieba，词元集合逐字节不变。
    if not CJK_RUN.search(text):
        return text
    return CJK_RUN.sub(lambda match: _segment_run(match.group()), text)


def cjk_terms(text: str) -> list[str]:
    """提取文本里的中文词，保持出现顺序去重。与 segment_cjk 用同一套切分。"""
    terms = []
    seen = set()
    for run in CJK_RUN.findall(text):
        for token in _segment_run(run).split():
            if token not in seen:
                seen.add(token)
                terms.append(token)
    return terms
