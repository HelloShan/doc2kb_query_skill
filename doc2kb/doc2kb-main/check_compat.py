"""
doc2kb — 兼容性检测 CLI（独立运行版）
======================================
委托给 convert.scan_compatibility 执行检测。

用法:
  python check_compat.py                    # 扫描默认源目录
  python check_compat.py --dir ../my_docs   # 指定目录
  
与 `python doc_pipeline.py check` 功能相同。
"""

import argparse
from pathlib import Path

try:
    from config import SOURCE_DIR
except ImportError:
    SOURCE_DIR = Path("../source_docs")

from convert import scan_compatibility


def main():
    parser = argparse.ArgumentParser(description="检测无法自动转换的文档")
    parser.add_argument("--dir", default="", help="扫描目录（默认使用 config 中的 SOURCE_DIR）")
    args = parser.parse_args()

    src_dir = Path(args.dir) if args.dir else SOURCE_DIR

    if not src_dir.exists():
        print(f"❌ 目录不存在: {src_dir}")
        return

    print(f"🔍 扫描目录: {src_dir.resolve()}")
    problems = scan_compatibility(src_dir)

    if not problems:
        print("✅ 所有文档兼容，无问题文件")
        return

    print(f"\n⚠  发现 {len(problems)} 个不兼容文件：\n")
    for fp, reason in problems:
        rel = fp.relative_to(src_dir)
        size = fp.stat().st_size / 1024
        print(f"  📄 {rel}  ({size:.0f} KB)")
        print(f"     原因: {reason}")
        print()

    print(f"共 {len(problems)} 个文件需手工处理，处理后重新运行流水线。")


if __name__ == "__main__":
    main()
