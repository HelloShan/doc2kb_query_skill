"""
测试 state.py：assert -> ValueError 的修复。
用 python -O 运行本文件会让 assert 全部失效，
如果修复正确（用 raise ValueError 而不是 assert），-O 下这个测试应该仍然通过。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from state import PipelineState, ST_OK, ST_ERROR


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


if __name__ == "__main__":
    test_invalid_conversion_status_raises_value_error()
    test_invalid_ingestion_status_raises_value_error()
    test_ok_status_with_zero_chunks_raises_value_error()
    test_valid_calls_still_work()
    print("\n全部通过 ✅")
