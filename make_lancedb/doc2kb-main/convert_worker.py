#!/usr/bin/env python3
"""
doc2kb — 单文件转换子进程 Worker（通用版，替代原 docling_worker.py）
========================================================================
把"单个文件的转换"整体隔离到独立子进程里执行，而不只是 Docling 调用本身。

────────────────────────────────────────────────────────────────────
背景（这是修复"build 卡死、Ctrl+C 后重跑还是卡死"问题的核心改动）
────────────────────────────────────────────────────────────────────
旧实现（docling_worker.py）只把 Docling 那一步放进子进程隔离，docx/pdf
在 Docling 失败后的原生降级（python-docx / pypdf），以及 xlsx/pptx
（本来就直接走原生解析，从不经过 Docling）全部是在
ThreadPoolExecutor 的工作线程里"裸跑"的，没有任何超时保护。

问题在于：Python 线程一旦在某个 C 扩展调用里卡死（比如遇到畸形 xlsx
触发 openpyxl 死循环、python-docx 解析损坏的 XML 卡死、docling
底层模型在个别文件上跑不出来），是没有办法从外部强制打断的——
`concurrent.futures.ThreadPoolExecutor` 的 `future.cancel()` 对"已经在
执行"的任务完全不起作用。

更致命的是：原代码的超时逻辑写在
    for future in as_completed(futures):
        result = future.result(timeout=timeout)
这个 `timeout` 只有在 `as_completed()` 已经把某个 future 判定为"完成"
之后才会被检查——如果同一时间卡死的文件数达到线程池大小
（CONVERT_WORKERS，默认 4），线程池里所有 worker 线程全部被卡死的任务
占满，后面排队的文件永远轮不到执行，`as_completed()` 会在没有任何
future 完成的情况下无限期阻塞，超时判断代码根本没有被执行的机会。
这正是"转换到某个文件后界面不再打印任何新文件名、Ctrl+C 之后重新跑
还是卡在原地"的根本原因：卡住的是操作系统线程，不是能被 Python 超时
逻辑感知到的东西；重跑时增量逻辑会优先重新处理上次没转换完的文件，
所以大概率立刻又撞上同一个"问题文件"，看起来像是"永远卡在那里"。

修复方式：把"整个单文件转换"（Docling + 各种原生降级 + md/txt/
code-config）都放进这个独立子进程里跑，父进程通过
`subprocess.run(timeout=...)` 等待——这是操作系统级别的超时，
在超时后会真正对子进程发送 SIGKILL 强制杀掉，线程池里对应的 worker
线程因此能够返回、被清空、继续去处理下一个排队的文件，流水线得以
持续推进，不会再无限期卡死。作为额外收益，任何原生扩展的段错误也只会
杀死这一个子进程，主进程和其它正在跑的文件不受影响。

用法： python convert_worker.py <source_path> <result_file>
"""
import sys
import json
import traceback
from pathlib import Path

# 确保能 import 到同目录下的 convert.py / config.py 等模块
# （子进程的当前工作目录不一定是脚本所在目录，必须显式加到 sys.path）
_SCRIPT_DIR = Path(__file__).parent.absolute()
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


def main():
    if len(sys.argv) < 3:
        print("用法: convert_worker.py <source_path> <result_file>", file=sys.stderr)
        sys.exit(1)

    source_path = sys.argv[1]
    result_file = sys.argv[2]

    try:
        from convert import convert_single_file
        result = convert_single_file(Path(source_path))
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        # 任何意外（比如 convert.py 自身 import 失败）都写出可读的错误信息，
        # 而不是让子进程裸崩溃、父进程只能拿到一个没有细节的 exitcode。
        try:
            with open(result_file, "w", encoding="utf-8") as f:
                json.dump({
                    "status": "error",
                    "error": f"convert_worker 子进程异常: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                }, f, ensure_ascii=False)
        except OSError:
            pass
        # 仍返回 exitcode 0：让父进程走"读取 result_file"的正常路径拿到
        # 详细错误信息，而不是走"exitcode 非 0"的兜底分支丢失细节。
        sys.exit(0)


if __name__ == "__main__":
    main()
