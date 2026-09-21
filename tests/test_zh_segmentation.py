"""中文切分本身的行为，以及"jieba 不进顶层 import"这条硬约束。"""

import subprocess
import sys
from pathlib import Path

import pytest

from wenjin.indexing.zh import CJK_RUN, cjk_terms, segment_cjk

ROOT = Path(__file__).resolve().parents[1]


def test_ascii_text_is_returned_byte_identical():
    for text in ["addDrawable", "osg::ref_ptr<Node> node", "a_b-c.d/e", "", "   ", "3.6.5"]:
        assert segment_cjk(text) == text


def test_chinese_run_becomes_space_separated_words():
    assert segment_cjk("本接口用于查询用户信息") == "本 接口 用于 查询 用户 信息"


def test_ascii_inside_chinese_text_is_preserved_verbatim():
    assert segment_cjk("Geode 如何添加 osg::Drawable？") == "Geode 如何 添加 osg::Drawable？"


def test_segmentation_is_deterministic():
    text = "本接口用于查询用户信息，Geode 如何添加 osg::Drawable？"
    assert segment_cjk(text) == segment_cjk(text) == segment_cjk(text)
    assert cjk_terms(text) == cjk_terms(text)


def test_cjk_terms_keep_order_and_dedupe():
    assert cjk_terms("接口说明和接口") == ["接口", "说明", "和"]


def test_cjk_terms_empty_for_ascii_only():
    assert cjk_terms("addDrawable Node") == []


@pytest.mark.parametrize("char", ["㐀", "一", "豈"])
def test_all_three_declared_ranges_are_matched(char):
    """扩展A + 基本区 + 兼容区，三段都要覆盖到。"""
    assert CJK_RUN.findall(f"a{char}b") == [char]


@pytest.mark.parametrize("text", ["addDrawable", "ref_ptr<Node>", "3.6.5", "a_b", "  "])
def test_cjk_range_does_not_swallow_ascii(text):
    """用 \\w 之类会误伤 ASCII；这里显式确认 ASCII 一个都不匹配。"""
    assert CJK_RUN.findall(text) == []


def test_jieba_is_not_imported_for_ascii_only_work():
    """jieba 是重依赖（建词典约 0.5 s），不能进任何顶层 import。

    子进程里跑，否则同一次 pytest 会话中别的测试已经 import 过 jieba，测不出东西。
    """
    code = "\n".join([
        "import sys",
        "import wenjin.indexing.bm25",
        "from wenjin.indexing.zh import segment_cjk, cjk_terms",
        "segment_cjk('pure ascii')",
        "cjk_terms('pure ascii')",
        "wenjin.indexing.bm25.query_terms('addDrawable')",
        "print('JIEBA_LOADED=' + str('jieba' in sys.modules))",
    ])
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert "JIEBA_LOADED=False" in result.stdout


def test_jieba_keeps_stdout_clean_for_stdio_transport():
    """jieba 初始化会打日志。MCP 的 stdio 传输用 stdout 传协议，被污染就废了。

    实测它走的是 stderr（`logging.StreamHandler(sys.stderr)`），这里锁住这个事实：
    换 jieba 版本或加 handler 把日志挪到 stdout 时，本测试会失败。
    """
    code = "\n".join([
        "import io, sys",
        "captured = io.StringIO()",
        "real_stdout = sys.stdout",
        "sys.stdout = captured",
        "from wenjin.indexing.zh import prewarm, segment_cjk",
        "prewarm()",
        "segment_cjk('测试')",
        "sys.stdout = real_stdout",
        "print('STDOUT=' + repr(captured.getvalue()))",
    ])
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert "STDOUT=''" in result.stdout
