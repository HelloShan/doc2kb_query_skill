"""
测试 state.py：assert -> ValueError 的修复。
用 python -O 运行本文件会让 assert 全部失效，
如果修复正确（用 raise ValueError 而不是 assert），-O 下这个测试应该仍然通过。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from state import PipelineState, ST_OK, ST_ERROR, ST_PENDING, ST_GARBLED


def test_invalid_conversion_status_raises_value_error():
    with tempfile.TemporaryDirectory() as td:
        state = PipelineState(Path(td) / "state.json")
        try:
            state.update_conversion("a.md", "not_a_real_status")
        except ValueError:
            pass
        else:
            raise AssertionError("应抛出 ValueError")
    print("test_invalid_conversion_status_raises_value_error OK")


def test_invalid_ingestion_status_raises_value_error():
    with tempfile.TemporaryDirectory() as td:
        state = PipelineState(Path(td) / "state.json")
        try:
            state.update_ingestion("a.md", "not_a_real_status")
        except ValueError:
            pass
        else:
            raise AssertionError("应抛出 ValueError")
    print("test_invalid_ingestion_status_raises_value_error OK")


def test_ok_status_with_zero_chunks_raises_value_error():
    with tempfile.TemporaryDirectory() as td:
        state = PipelineState(Path(td) / "state.json")
        try:
            state.update_ingestion("a.md", ST_OK, chunks=0)
        except ValueError:
            pass
        else:
            raise AssertionError("应抛出 ValueError（ok 状态但 chunks=0）")
    print("test_ok_status_with_zero_chunks_raises_value_error OK")


def test_valid_calls_still_work():
    with tempfile.TemporaryDirectory() as td:
        state = PipelineState(Path(td) / "state.json")
        state.update_conversion("a.md", ST_OK, md_path="output_md/a.md")
        state.update_ingestion("a.md", ST_OK, chunks=3)
        state.update_conversion("b.md", ST_ERROR, error="解析失败")
        entry = state.get_file_state("a.md")
        assert entry["conversion"]["status"] == ST_OK
        assert entry["ingestion"]["chunks"] == 3
    print("test_valid_calls_still_work OK")


def test_reset_file_does_not_clear_successful_conversion():
    """
    回归测试：修复"retry 把刚转换成功、但还没入库的文件重新打回 pending，
    进度永远攒不起来"这个 bug。

    背景：--convert-only 场景下入库阶段还没跑，文件的 ingestion.status
    天然就是 pending。get_failed_files("all") 把 pending 也算作"需要
    重试"是有意为之的（这样完整 retry 才能顺带把还没入库的文件带上），
    但 reset_file() 之前会无条件把 conversion 和 ingestion 一起清空——
    结果就是任何"转换已经成功、只是还没入库"的文件，一旦被 retry 扫到，
    转换状态也被一起打回 pending，白白浪费已经做完的工作，而且每 retry
    一次都会重演一遍，进度永远也攒不起来。
    """
    with tempfile.TemporaryDirectory() as td:
        state = PipelineState(Path(td) / "state.json")
        state.init_file("a.docx", sha256="abc", size=100, mtime="2026-01-01")
        state.update_conversion("a.docx", ST_OK, md_path="output_md/a.md")
        # 此时 ingestion 仍是初始的 pending（还没跑到入库阶段）

        failed = state.get_failed_files("all")
        assert any(rel == "a.docx" for rel, _ in failed), \
            "get_failed_files 应该把'转换成功但还没入库'的文件也纳入（这样完整 retry 才能带上它去入库）"

        state.reset_file("a.docx")
        entry = state.get_file_state("a.docx")
        assert entry["conversion"]["status"] == ST_OK, \
            "reset_file 不应该清空已经成功的转换状态！"
        assert entry["ingestion"]["status"] == ST_PENDING
    print("test_reset_file_does_not_clear_successful_conversion OK")


def test_reset_file_does_not_clear_successful_ingestion():
    """反过来的场景：入库已经成功，只是转换记录因为别的原因需要重置
    （理论上不该发生，但防御性地验证 reset_file 是按阶段独立判断的，
    不会因为一个阶段需要重置就连带清空另一个已经成功的阶段）。"""
    with tempfile.TemporaryDirectory() as td:
        state = PipelineState(Path(td) / "state.json")
        state.init_file("a.docx", sha256="abc", size=100, mtime="2026-01-01")
        state.update_conversion("a.docx", ST_GARBLED, error="疑似乱码")
        state.update_ingestion("a.docx", ST_OK, chunks=5)

        state.reset_file("a.docx")
        entry = state.get_file_state("a.docx")
        assert entry["conversion"]["status"] == ST_PENDING, "garbled 应该被重置，等待重新转换"
        assert entry["ingestion"]["status"] == ST_OK, "已经成功入库的状态不该被连带清空"
        assert entry["ingestion"]["chunks"] == 5
    print("test_reset_file_does_not_clear_successful_ingestion OK")


def test_reset_file_resets_genuine_failure():
    """真正失败的文件，reset_file 应该正常把它重置为 pending 等待重试。"""
    with tempfile.TemporaryDirectory() as td:
        state = PipelineState(Path(td) / "state.json")
        state.init_file("a.docx", sha256="abc", size=100, mtime="2026-01-01")
        state.update_conversion("a.docx", ST_ERROR, error="解析失败")

        state.reset_file("a.docx")
        entry = state.get_file_state("a.docx")
        assert entry["conversion"]["status"] == ST_PENDING
    print("test_reset_file_resets_genuine_failure OK")


def test_get_failed_files_excludes_fully_completed_files():
    """两个阶段都成功的文件，不应该出现在 get_failed_files 的结果里
    （否则每次 retry 都会重新处理所有文件，增量逻辑形同虚设）。"""
    with tempfile.TemporaryDirectory() as td:
        state = PipelineState(Path(td) / "state.json")
        state.init_file("a.docx", sha256="abc", size=100, mtime="2026-01-01")
        state.update_conversion("a.docx", ST_OK, md_path="output_md/a.md")
        state.update_ingestion("a.docx", ST_OK, chunks=3)

        failed = state.get_failed_files("all")
        assert not any(rel == "a.docx" for rel, _ in failed), \
            "两个阶段都成功的文件不应该被当成'需要重试'"
    print("test_get_failed_files_excludes_fully_completed_files OK")


if __name__ == "__main__":
    test_invalid_conversion_status_raises_value_error()
    test_invalid_ingestion_status_raises_value_error()
    test_ok_status_with_zero_chunks_raises_value_error()
    test_valid_calls_still_work()
    test_reset_file_does_not_clear_successful_conversion()
    test_reset_file_does_not_clear_successful_ingestion()
    test_reset_file_resets_genuine_failure()
    test_get_failed_files_excludes_fully_completed_files()
    print("\n全部通过 ✅")
