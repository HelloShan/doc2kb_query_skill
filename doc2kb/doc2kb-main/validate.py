"""
doc2kb — 文档内容有效性验证模块
==================================
核心功能：检测 PDF→MD 等转换产生的乱码/二进制垃圾。
可独立使用，也可被 convert.py 和 ingest.py 调用。

核心洞察：
  乱码文档解码成 UTF-8 后会产生随机 CJK 字符，
  但几乎不包含真实的中文标点（。，、；：？！—…）。
  正常文档在前 2000 字节中至少有 3 个中文标点。
"""

from pathlib import Path

# 中文标点集合（用于乱码检测）
_CHINESE_PUNCT = frozenset("。，、；：？！—…·「」『』【】（）《》〈〉" "．，、；：？！—…·「」『』【】（）《》〈〉")

# 文档头部的常见模板（用于辅助判断）
_TEMPLATES = [
    '技  术  文  件', '技术文件', '<!-- image -->', '<!--image-->',
    '# ', '## ', '### ', '| ---', '|序号', '| 序号',
    '目  录', '目录', '第1章', '第 1 章', '第一章',
    '1. ', '1）', '1、', '前  言', '前言',
    '版  本', '版本', '修订记录', '修  订  记  录',
]


def is_content_readable(text: str, check_bytes: int = 2000,
                        punct_threshold: int = 3) -> bool:
    """
    判断文本内容是否可读（非乱码）。

    Parameters
    ----------
    text : str
        待检测的文本内容。
    check_bytes : int
        检查前 N 个字符（实际是字符数）。
    punct_threshold : int
        中文标点数量阈值，≥此值视为正常文档。

    Returns
    -------
    bool
        True = 可读（正常文档）, False = 疑似乱码。
    """
    if not text or not text.strip():
        return False

    head = text[:check_bytes]

    # 1. 中文标点计数法（最高效、最准确）
    punct_count = sum(1 for c in head if c in _CHINESE_PUNCT)
    if punct_count >= punct_threshold:
        return True

    # 2. 快速模板匹配（辅助判断）
    for t in _TEMPLATES:
        if t in head:
            return True

    # 3. 安全网：如果包含大量正常 ASCII 单词也可接受
    #    （针对纯英文技术文档）
    ascii_words = sum(1 for w in head.split() if w.isascii() and len(w) > 2)
    if ascii_words >= 10:
        return True

    return False


def is_file_readable(file_path: Path, check_bytes: int = 2000,
                     punct_threshold: int = 3) -> bool:
    """
    判断文件内容是否可读（非乱码）。
    是 is_content_readable 的文件版本封装。

    Parameters
    ----------
    file_path : Path
        要检查的文件路径。
    check_bytes : int
        检查前 N 字节解码为字符后的长度。
    punct_threshold : int
        中文标点数量阈值。

    Returns
    -------
    bool
        True = 可读, False = 疑似乱码。
    """
    if not file_path.exists():
        return False

    try:
        raw = file_path.read_bytes()
        text = raw.decode('utf-8', errors='replace')
        return is_content_readable(text, check_bytes, punct_threshold)
    except Exception:
        return False


def count_chinese_punct(text: str) -> int:
    """统计文本中的中文标点数量。"""
    return sum(1 for c in text if c in _CHINESE_PUNCT)


def get_garbled_ratio(text: str, check_bytes: int = 2000) -> float:
    """
    计算文档"乱码度"——非 ASCII、非 CJK 统一表意文字、
    非标准标点的"混乱字符"占比。值越高越可能是乱码。

    用于排序/筛选，不做硬阈值判断。
    """
    import unicodedata

    head = text[:check_bytes]
    if not head:
        return 1.0

    messy = 0
    for c in head:
        cat = unicodedata.category(c)
        # 忽略常见字符类别
        if cat.startswith('L') or cat.startswith('N'):  # 字母/数字
            continue
        if cat.startswith('P'):  # 标点
            continue
        if cat.startswith('Z'):  # 空白
            continue
        messy += 1

    return messy / len(head)
