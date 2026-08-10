"""
测试 glossary.py：术语表加载与查询展开。

glossary.py 现在不依赖 config.py，path 由调用方显式传入
（调用方各自决定用哪个路径的术语表，这个模块自己不读任何全局配置）。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import glossary  # noqa: E402


def _reset_glossary_cache():
    glossary._cache = {}


def test_missing_glossary_file_is_noop():
    with tempfile.TemporaryDirectory() as td:
        gpath = Path(td) / "does_not_exist.yaml"
        _reset_glossary_cache()
        assert glossary.load_glossary(gpath) == {}
        q = "KPI 是什么"
        assert glossary.expand_query(q, gpath) == q
    print("test_missing_glossary_file_is_noop OK")


def test_glossary_expands_known_abbreviation():
    with tempfile.TemporaryDirectory() as td:
        gpath = Path(td) / "glossary.yaml"
        gpath.write_text(
            "KPI:\n  full: 关键绩效指标\n  aliases: [Key Performance Indicator]\n",
            encoding="utf-8",
        )
        _reset_glossary_cache()

        g = glossary.load_glossary(gpath)
        assert "KPI" in g
        assert g["KPI"]["full"] == "关键绩效指标"

        expanded = glossary.expand_query("KPI 的定义是什么", gpath)
        assert "KPI 的定义是什么" in expanded  # 原始问题完整保留
        assert "关键绩效指标" in expanded       # 全称被追加
        assert "Key Performance Indicator" in expanded  # 别名也被追加
    print("test_glossary_expands_known_abbreviation OK")


def test_glossary_simple_string_form():
    with tempfile.TemporaryDirectory() as td:
        gpath = Path(td) / "glossary.yaml"
        gpath.write_text("SLA: 服务级别协议\n", encoding="utf-8")
        _reset_glossary_cache()

        expanded = glossary.expand_query("SLA 违约怎么处理", gpath)
        assert "服务级别协议" in expanded
    print("test_glossary_simple_string_form OK")


def test_glossary_does_not_match_substring_of_other_word():
    """KPI 不应该匹配到比如 'SKPI' 这种词的一部分（ASCII 术语按单词边界匹配）。"""
    with tempfile.TemporaryDirectory() as td:
        gpath = Path(td) / "glossary.yaml"
        gpath.write_text("KPI:\n  full: 关键绩效指标\n  aliases: []\n", encoding="utf-8")
        _reset_glossary_cache()

        expanded = glossary.expand_query("SKPI999 是什么系统", gpath)
        assert "关键绩效指标" not in expanded
    print("test_glossary_does_not_match_substring_of_other_word OK")


def test_glossary_no_match_returns_original_unchanged():
    with tempfile.TemporaryDirectory() as td:
        gpath = Path(td) / "glossary.yaml"
        gpath.write_text("KPI:\n  full: 关键绩效指标\n  aliases: []\n", encoding="utf-8")
        _reset_glossary_cache()

        q = "今天天气怎么样"
        assert glossary.expand_query(q, gpath) == q
    print("test_glossary_no_match_returns_original_unchanged OK")


def test_malformed_glossary_degrades_to_noop():
    with tempfile.TemporaryDirectory() as td:
        gpath = Path(td) / "glossary.yaml"
        gpath.write_text("this is not: [valid, yaml, dict, structure", encoding="utf-8")
        _reset_glossary_cache()

        # 不应该抛异常，应该静默降级为空表
        g = glossary.load_glossary(gpath)
        assert g == {}
        q = "随便问点什么"
        assert glossary.expand_query(q, gpath) == q
    print("test_malformed_glossary_degrades_to_noop OK")


def test_glossary_cache_reloads_on_file_change():
    with tempfile.TemporaryDirectory() as td:
        gpath = Path(td) / "glossary.yaml"
        gpath.write_text("A: 甲\n", encoding="utf-8")
        _reset_glossary_cache()

        g1 = glossary.load_glossary(gpath)
        assert "A" in g1 and "B" not in g1

        import time
        time.sleep(0.05)
        gpath.write_text("A: 甲\nB: 乙\n", encoding="utf-8")
        g2 = glossary.load_glossary(gpath)
        assert "B" in g2, "文件修改后应该自动重新加载（按 mtime 判断），而不是一直用旧缓存"
    print("test_glossary_cache_reloads_on_file_change OK")


def test_glossary_different_paths_cached_independently():
    """两个不同路径的术语表应该分别独立缓存，互不覆盖。"""
    with tempfile.TemporaryDirectory() as td:
        path_a = Path(td) / "a.yaml"
        path_b = Path(td) / "b.yaml"
        path_a.write_text("A: 甲\n", encoding="utf-8")
        path_b.write_text("B: 乙\n", encoding="utf-8")
        _reset_glossary_cache()

        ga = glossary.load_glossary(path_a)
        gb = glossary.load_glossary(path_b)
        assert "A" in ga and "B" not in ga
        assert "B" in gb and "A" not in gb
    print("test_glossary_different_paths_cached_independently OK")


if __name__ == "__main__":
    test_missing_glossary_file_is_noop()
    test_glossary_expands_known_abbreviation()
    test_glossary_simple_string_form()
    test_glossary_does_not_match_substring_of_other_word()
    test_glossary_no_match_returns_original_unchanged()
    test_malformed_glossary_degrades_to_noop()
    test_glossary_cache_reloads_on_file_change()
    test_glossary_different_paths_cached_independently()
    print("\n全部通过 ✅")
