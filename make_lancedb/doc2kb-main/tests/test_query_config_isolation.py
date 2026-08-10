"""
测试 query_reference/server.py 里两个新加的安全机制：

1. 向量维度运行时自检 —— 构建端和查询端现在是完全独立的两份配置，
   如果两边 EMBEDDING_MODEL 配得不一致，_validate_vector_dim() 应该
   在第一次真正连接知识库时就用清楚的报错拦下来，而不是让它在某次
   查询里于 pyarrow 深层调用中炸出一个看不懂的维度不匹配错误。

2. 访问口令鉴权 —— 设置了 DOC2KB_QUERY_AUTH_TOKEN 后，缺少/错误的
   Authorization 请求头应该被拒绝；没设置口令时应该像以前一样直接放行
   （向后兼容默认行为）。
"""
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "query_reference"))

import numpy as np
import lancedb
import pyarrow as pa

import server  # noqa: E402


class _FixedDimStubModel:
    """打桩模型：query_embed 返回固定维度的向量，用于测试维度自检。"""

    def __init__(self, dim):
        self.dim = dim

    def query_embed(self, texts):
        for _ in texts:
            yield np.zeros(self.dim, dtype=np.float32)


def _build_temp_kb(db_path: Path, vector_dim: int):
    db = lancedb.connect(str(db_path))
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), vector_dim)),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("file_hash", pa.string()),
        pa.field("chunk_index", pa.int32()),
        pa.field("chunk_total", pa.int32()),
    ])
    table = db.create_table("docs", schema=schema)
    table.add([{
        "vector": [0.0] * vector_dim, "text": "占位内容", "source": "a.md",
        "file_hash": "x", "chunk_index": 0, "chunk_total": 1,
    }])
    table.create_fts_index("text", replace=True)
    return table


def test_matching_dimensions_pass_validation(monkeypatch):
    """查询端模型输出维度和库里存的一致时，不应该报错。"""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(tmp_dir) / "kb.lancedb"
        _build_temp_kb(db_path, vector_dim=512)

        monkeypatch.setattr(server, "DB_PATH", str(db_path))
        monkeypatch.setattr(server, "TABLE_NAME", "docs")
        monkeypatch.setattr(server, "_get_embedding_model", lambda: _FixedDimStubModel(512))
        server._dim_checked = False

        db, table = server._get_db_and_table()
        assert table is not None
        assert server._dim_checked is True
    finally:
        server._dim_checked = False
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_matching_dimensions_pass_validation OK")


def test_mismatched_dimensions_raise_clear_error(monkeypatch):
    """
    查询端配置的模型输出维度和库里存的不一致时（模拟两边 EMBEDDING_MODEL
    配置漂移的场景），应该立刻报一个说清楚原因的错误，而不是在写入/深层
    调用里炸出一个看不懂的 pyarrow 错误，也不能安静地放过去导致查询结果乱掉。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(tmp_dir) / "kb.lancedb"
        _build_temp_kb(db_path, vector_dim=1024)  # 库是 1024 维建的

        monkeypatch.setattr(server, "DB_PATH", str(db_path))
        monkeypatch.setattr(server, "TABLE_NAME", "docs")
        # 但查询端配置的模型只输出 512 维——模拟配置漂移
        monkeypatch.setattr(server, "_get_embedding_model", lambda: _FixedDimStubModel(512))
        server._dim_checked = False

        try:
            server._get_db_and_table()
            raise AssertionError("维度不匹配时应该抛异常，不应该安静地放过去")
        except ValueError as e:
            msg = str(e)
            assert "512" in msg and "1024" in msg, f"报错信息应该包含两边的实际维度，实际: {msg}"
            assert "EMBEDDING_MODEL" in msg, f"报错信息应该提示是模型配置不一致，实际: {msg}"
    finally:
        server._dim_checked = False
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_mismatched_dimensions_raise_clear_error OK")


def test_dimension_check_only_runs_once(monkeypatch):
    """自检只应该在进程生命周期内跑一次，不是每次查询都重新探测一遍。"""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(tmp_dir) / "kb.lancedb"
        _build_temp_kb(db_path, vector_dim=512)

        call_count = {"n": 0}

        class _CountingModel(_FixedDimStubModel):
            def query_embed(self, texts):
                call_count["n"] += 1
                yield from super().query_embed(texts)

        monkeypatch.setattr(server, "DB_PATH", str(db_path))
        monkeypatch.setattr(server, "TABLE_NAME", "docs")
        monkeypatch.setattr(server, "_get_embedding_model", lambda: _CountingModel(512))
        server._dim_checked = False

        server._get_db_and_table()
        server._get_db_and_table()
        server._get_db_and_table()

        assert call_count["n"] == 1, f"自检探针只应该跑 1 次，实际跑了 {call_count['n']} 次"
    finally:
        server._dim_checked = False
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_dimension_check_only_runs_once OK")


def test_auth_disabled_by_default_allows_any_request():
    original_token = server.QUERY_AUTH_TOKEN
    server.QUERY_AUTH_TOKEN = ""
    try:
        assert server._check_auth(None) is True
        assert server._check_auth("garbage") is True
        assert server._check_auth("Bearer wrong-token") is True
    finally:
        server.QUERY_AUTH_TOKEN = original_token
    print("test_auth_disabled_by_default_allows_any_request OK")


def test_auth_enabled_rejects_missing_or_wrong_token():
    original_token = server.QUERY_AUTH_TOKEN
    server.QUERY_AUTH_TOKEN = "secret-token-123"
    try:
        assert server._check_auth(None) is False
        assert server._check_auth("") is False
        assert server._check_auth("secret-token-123") is False, "缺少 'Bearer ' 前缀应该拒绝"
        assert server._check_auth("Bearer wrong-token") is False
        assert server._check_auth("Bearer secret-token-123") is True
    finally:
        server.QUERY_AUTH_TOKEN = original_token
    print("test_auth_enabled_rejects_missing_or_wrong_token OK")


class _FakeMonkeypatch:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


if __name__ == "__main__":
    for test_fn in [
        test_matching_dimensions_pass_validation,
        test_mismatched_dimensions_raise_clear_error,
        test_dimension_check_only_runs_once,
    ]:
        mp = _FakeMonkeypatch()
        try:
            test_fn(mp)
        finally:
            mp.undo()

    test_auth_disabled_by_default_allows_any_request()
    test_auth_enabled_rejects_missing_or_wrong_token()
    print("\n全部通过 ✅")
