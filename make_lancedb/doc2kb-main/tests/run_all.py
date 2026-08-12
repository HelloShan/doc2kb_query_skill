#!/usr/bin/env python3
"""一键运行 tests/ 下全部回归测试脚本，任何一个失败则以非零状态码退出。"""
import subprocess
import sys
from pathlib import Path

TESTS = [
    "test_state.py",
    "test_convert_docm.py",
    "test_ingest_abandonment.py",
    "test_chunking.py",
    "test_ingest_doctype.py",
    "test_glossary.py",
    "test_env_config.py",
]

if __name__ == "__main__":
    here = Path(__file__).parent
    failed = []
    for t in TESTS:
        print(f"\n{'=' * 60}\n{t}\n{'=' * 60}")
        proc = subprocess.run([sys.executable, str(here / t)])
        if proc.returncode != 0:
            failed.append(t)

    print(f"\n{'=' * 60}")
    if failed:
        print(f"❌ 失败: {failed}")
        sys.exit(1)
    else:
        print(f"✅ 全部 {len(TESTS)} 个测试文件通过")
