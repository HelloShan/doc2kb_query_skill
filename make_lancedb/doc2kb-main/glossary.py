"""
doc2kb — 术语表模块
======================
可选功能：人工维护一份缩写/术语 -> 全称映射表（glossary.yaml），
查询时自动把问题里出现的术语展开成"缩写 + 全称"一起送去检索，
缓解"问题里只写缩写、知识库原文写的是全称"（或反过来）导致的召回不足。

不维护 glossary.yaml 时这个模块完全是无操作（no-op），不影响任何现有流程。

故意不 import config.py：调用方（比如 query_reference/server.py）自己
决定用哪个路径的术语表，这个模块只管加载和展开，不关心配置从哪来。

glossary.yaml 格式（见项目根目录 glossary.example.yaml）：
    KPI:
      full: 关键绩效指标
      aliases: [关键绩效指标, Key Performance Indicator]
    ZTE:
      full: 中兴通讯
      aliases: []
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Optional

_lock = threading.Lock()
# 按路径分别缓存：{resolved_path_str: (glossary_dict, mtime)}
_cache: dict[str, tuple[dict, Optional[float]]] = {}


def load_glossary(path, force_reload: bool = False) -> dict:
    """
    加载指定路径的 glossary.yaml，返回 {term: {"full": str, "aliases": [str, ...]}}。
    文件不存在、为空或格式非法时返回 {}，不抛异常（这是可选功能，不能因为
    术语表写错格式就把整个查询流程搞挂）。
    结果按 (路径, 文件 mtime) 缓存，文件改了会自动重新加载；不同路径分别
    独立缓存，互不影响。
    """
    key = str(Path(path).resolve()) if path else ""
    resolved = Path(path) if path else None

    with _lock:
        if resolved is None or not resolved.exists():
            _cache[key] = ({}, None)
            return _cache[key][0]

        mtime = resolved.stat().st_mtime
        cached = _cache.get(key)
        if not force_reload and cached is not None and cached[1] == mtime:
            return cached[0]

        try:
            import yaml
            raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                _cache[key] = ({}, mtime)
                return _cache[key][0]

            parsed = {}
            for term, info in raw.items():
                if not isinstance(term, str):
                    continue
                if isinstance(info, dict):
                    full = info.get("full", "")
                    aliases = info.get("aliases", []) or []
                elif isinstance(info, str):
                    full, aliases = info, []
                else:
                    continue
                parsed[term] = {
                    "full": full,
                    "aliases": [a for a in aliases if isinstance(a, str)],
                }
            _cache[key] = (parsed, mtime)
        except Exception:
            # 术语表格式写错了：静默降级为空表，不影响主流程
            _cache[key] = ({}, mtime)

    return _cache[key][0]


def _term_pattern(term: str) -> re.Pattern:
    # 纯 ASCII 缩写（如 KPI/ZTE）按"单词边界"匹配，避免匹配到别的单词里的子串；
    # 含中文的术语没有单词边界概念，直接按子串匹配。
    if term.isascii():
        return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    return re.compile(re.escape(term))


def expand_query(question: str, path) -> str:
    """
    在问题里查找已知术语，命中则把"全称 + 别名"追加到问题末尾一起用于检索
    （embedding 向量化 + FTS 关键词匹配都用展开后的文本）。

    只做"追加"、不做"替换"：保留原始措辞的同时补充可能对不上的另一种说法，
    对向量语义检索和 BM25 关键词检索都有帮助，且不会因为展开出错而丢失
    原始问题信息。

    path: 术语表文件路径，由调用方传入（这个模块自己不读任何全局配置）。
    """
    glossary = load_glossary(path)
    if not glossary or not question:
        return question

    extra_terms = []
    seen = set()
    for term, info in glossary.items():
        pattern = _term_pattern(term)
        if not pattern.search(question):
            continue
        for candidate in [info.get("full", "")] + info.get("aliases", []):
            candidate = candidate.strip()
            if candidate and candidate not in seen and candidate.lower() != term.lower():
                seen.add(candidate)
                extra_terms.append(candidate)

    if not extra_terms:
        return question

    return question + " " + " ".join(extra_terms)
