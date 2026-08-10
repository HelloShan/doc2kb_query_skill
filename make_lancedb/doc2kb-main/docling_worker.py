#!/usr/bin/env python3
"""
doc2kb — Docling 子进程转换器
===============================
独立脚本，通过 subprocess.run() 调用。
如果 docling segfault，只有这个子进程死掉，主流水线不受影响。

用法： python docling_worker.py <source_path> <output_path> <result_file>
"""
import sys
import json
import os

# 启动时先设好镜像源
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"


def main():
    if len(sys.argv) < 4:
        print("用法: docling_worker.py <source_path> <output_path> <result_file>", file=sys.stderr)
        sys.exit(1)

    source_path = sys.argv[1]
    output_path = sys.argv[2]
    result_file = sys.argv[3]

    try:
        from docling.document_converter import DocumentConverter
        conv = DocumentConverter()
        result = conv.convert(source_path)
        md_content = result.document.export_to_markdown()
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        with open(result_file, 'w') as f:
            json.dump({"status": "ok"}, f)
    except Exception as e:
        with open(result_file, 'w') as f:
            json.dump({"status": "error", "error": f"{type(e).__name__}: {e}"}, f)
        sys.exit(2)


if __name__ == "__main__":
    main()
