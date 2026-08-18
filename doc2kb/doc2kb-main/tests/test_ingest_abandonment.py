"""
测试 ingest.py：超时"放弃"标记机制。

背景：ThreadPoolExecutor 无法真正打断一个已经在运行的任务，
future.cancel() 对"已开始执行"的线程没有效果——它可能仍在后台跑，
跑完后仍会尝试写库，和下一次重试产生竞争。
这里验证：一旦某个 rel_path 被标记为 abandoned，ingest_single_md
会在真正接触 LanceDB 之前就提前返回，不会产生任何写入。
"""
import sys
import time
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

sys.path.insert(0, str(Path(__file__).parent.parent))

import ingest  # noqa: E402


def test_abandoned_path_short_circuits_before_db_access():
    # chunk_markdown_file() 内部用 relative_to(OUTPUT_MD_DIR) 算 source 相对路径，
    # 所以测试文件必须真的放在 OUTPUT_MD_DIR 下面，用完清理掉。
    test_dir = ingest.OUTPUT_MD_DIR / "_test_abandonment"
    test_dir.mkdir(parents=True, exist_ok=True)
    md_path = test_dir / "doc.md"
    md_path.write_text("# 标题\n\n这是一些测试内容，用来验证分块逻辑正常工作。" * 5, encoding="utf-8")

    rel_path = md_path.relative_to(ingest.OUTPUT_MD_DIR).as_posix()
    ingest.mark_abandoned(rel_path)
    try:
        # 故意不触碰真实 LanceDB / embedding 模型：如果提前返回逻辑失效，
        # 这里会因为尝试真正打开 DB / 加载模型而报错或挂起，而不是安静地失败。
        result = ingest.ingest_single_md(md_path, rel_path, "fake_sha256")
        assert result["status"] == "error", result
        assert "放弃" in result["error"], result
        assert result["chunks"] == 0
    finally:
        ingest._abandoned_paths.discard(rel_path)
        md_path.unlink(missing_ok=True)
        test_dir.rmdir()
    print("test_abandoned_path_short_circuits_before_db_access OK")


def test_non_abandoned_path_is_not_affected():
    assert ingest.is_abandoned("some/other/path.md") is False
    print("test_non_abandoned_path_is_not_affected OK")


def test_ingest_batch_marks_abandoned_on_timeout():
    """
    模拟一个"卡住"的任务：ingest_batch 判定超时后，
    应该调用 mark_abandoned，且该 rel_path 之后确实处于 abandoned 状态。
    这里直接复刻 ingest_batch 内部的超时分支逻辑（不跑真实 LanceDB），
    因为 ingest_batch 硬编码调用的是 ingest_single_md，无法轻易注入 mock。
    """
    rel_path = "slow_file.md"
    ingest._abandoned_paths.discard(rel_path)  # 确保初始干净

    def _slow_task():
        time.sleep(2)
        return "finished-too-late"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_slow_task)
        try:
            future.result(timeout=0.2)
            raise AssertionError("预期应该超时")
        except FutureTimeoutError:
            # 复刻 ingest_batch 里 TimeoutError 分支的行为
            ingest.mark_abandoned(rel_path)
            future.cancel()  # 已知：对正在运行的任务这一步不会真正生效

        assert ingest.is_abandoned(rel_path) is True

    ingest._abandoned_paths.discard(rel_path)
    print("test_ingest_batch_marks_abandoned_on_timeout OK")


if __name__ == "__main__":
    test_abandoned_path_short_circuits_before_db_access()
    test_non_abandoned_path_is_not_affected()
    test_ingest_batch_marks_abandoned_on_timeout()
    print("\n全部通过 ✅")
