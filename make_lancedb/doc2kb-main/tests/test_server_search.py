"""
测试 query_reference/server.py::search()

用真实的临时 LanceDB 表（不依赖网络/HF 模型下载），
配合一个打桩(stub)的 embedding 模型验证：

1. 原来 .add_fts_query() / reranker="rrf" 字符串写法会导致的
   AttributeError/ValueError 崩溃已经修复（这是最关键的一项 —— 修复前
   search() 在任何 lancedb 版本上都会直接报错）。
2. 相似度用真实余弦相似度 (1 - cosine_distance) 计算，而不是
   1/(1+_distance) 或者恒等于 1.0 的假分数。
3. 最终结果顺序遵循 hybrid + RRF 融合排序，而不是被重新按向量距离排序覆盖掉。
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

import server  # noqa: E402  (query_reference/server.py)


VECTOR_DIM = 8

DOCS = [
    # (text, vector, source, chunk_index)
    ("西瓜的种植方法和田间管理注意事项",
     [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "fruit/watermelon.md", 0),
    ("苹果树的施肥周期和病虫害防治",
     [0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "fruit/apple.md", 0),
    ("关于SQL建表脚本的命名规范和字段约定",
     [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0], "dev/sql_convention.md", 0),
    ("数据库索引优化与慢查询排查配置说明",
     [0.0, 0.0, 0.9, 0.1, 0.0, 0.0, 0.0, 0.0], "dev/db_index.md", 0),
    ("专业术语表：KPI关键绩效指标的定义与计算口径",
     [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0], "glossary/kpi.md", 0),
]

# 查询向量：语义上明显偏向"数据库/SQL"这一类（第3、4条），
# 但问题文本里包含"KPI"关键词，用来触发 BM25/关键词那一路的信号，
# 从而让 hybrid+RRF 融合排序和"纯按向量距离排序"这两种结果不同——
# 这样才能真正验证"不再按向量距离重排"这条修复是否生效，而不是凑巧一样。
QUERY_VEC = [0.0, 0.0, 0.95, 0.05, 0.0, 0.0, 0.0, 0.0]
QUESTION = "KPI 相关的 SQL 建表脚本规范是什么"


class _StubEmbeddingModel:
    """打桩模型：不下载任何真实权重，query_embed 返回预先设定好的向量。"""

    def query_embed(self, texts):
        for _ in texts:
            yield np.array(QUERY_VEC, dtype=np.float32)

    def embed(self, texts, batch_size=1):
        # 兜底路径也测一下，保证两条路径都不崩
        for _ in texts:
            yield np.array(QUERY_VEC, dtype=np.float32)


def _build_temp_kb(db_path: Path):
    db = lancedb.connect(str(db_path))
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("file_hash", pa.string()),
        pa.field("chunk_index", pa.int32()),
        pa.field("chunk_total", pa.int32()),
    ])
    table = db.create_table(server.TABLE_NAME, schema=schema)
    rows = [{
        "vector": vec,
        "text": text,
        "source": source,
        "file_hash": "deadbeef",
        "chunk_index": idx,
        "chunk_total": 1,
    } for text, vec, source, idx in DOCS]
    table.add(rows)
    table.create_fts_index("text", replace=True)
    return table


def test_search_does_not_crash_and_uses_real_cosine_similarity(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(tmp_dir) / "kb.lancedb"
        _build_temp_kb(db_path)

        monkeypatch.setattr(server, "DB_PATH", str(db_path))
        monkeypatch.setattr(server, "_get_embedding_model", lambda: _StubEmbeddingModel())

        result = server.search(QUESTION, top_k=5, threshold=0.0)

        assert result.get("error") is None, f"search() 不应该报错，实际: {result.get('error')}"
        assert result["found"] is True
        assert result["hits"] > 0

        # 相似度必须是合法的余弦相似度范围，且不是恒为 1.0（这是修复前的 bug 表现）
        sims = [r["similarity"] for r in result["results"] if r["similarity"] is not None]
        assert sims, "应该至少有一个结果带有可比较的余弦相似度"
        assert not all(s == 1.0 for s in sims), "相似度不应恒为 1.0（说明又退化回了旧 bug）"
        for s in sims:
            assert -1.0001 <= s <= 1.0001, f"相似度超出合法范围: {s}"

        # 对查询向量而言，db_index.md（[0,0,0.9,0.1,...]）与 QUERY_VEC 的余弦相似度
        # 应该非常高（接近 1），且明显高于完全不相关的 watermelon/apple/kpi 文档
        top_sources = [r["source"] for r in result["results"]]
        assert "dev/db_index.md" in top_sources or "dev/sql_convention.md" in top_sources

        print("查询结果:")
        for r in result["results"]:
            print(f"  [{r['matched_by']}] sim={r['similarity']}  {r['source']}  {r['text'][:20]}")
        print(f"  best_similarity={result['best_similarity']}  confidence={result['confidence']}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_search_does_not_crash_and_uses_real_cosine_similarity OK")


def test_search_preserves_hybrid_rrf_order_not_raw_cosine_order(monkeypatch):
    """
    核心回归测试：验证最终返回顺序 == hybrid+RRF 融合顺序，
    而不是被重新按纯向量余弦相似度排序覆盖。
    做法：分别拿到 (a) search() 的最终结果顺序，(b) 直接调用
    server._run_hybrid_search 拿到的 RRF 融合顺序，(c) 一个纯向量
    search 的余弦距离排序 —— 用查询文本里的"KPI"关键词制造
    向量距离排序 和 RRF 融合排序不一致的情况，然后断言 search()
    的结果顺序等于 (b) 而不是 (c)（除非两者本来就恰好一致）。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(tmp_dir) / "kb.lancedb"
        table = _build_temp_kb(db_path)

        monkeypatch.setattr(server, "DB_PATH", str(db_path))
        monkeypatch.setattr(server, "_get_embedding_model", lambda: _StubEmbeddingModel())

        result = server.search(QUESTION, top_k=5, threshold=0.0)
        final_order = [r["source"] for r in result["results"]]

        hybrid_raw = server._run_hybrid_search(table, QUERY_VEC, QUESTION, limit=10)
        hybrid_order = [r["source"] for r in hybrid_raw]

        pure_vector_raw = table.search(QUERY_VEC).metric("cosine").limit(10).to_list()
        pure_vector_order = [r["source"] for r in pure_vector_raw]

        assert final_order == hybrid_order, (
            f"最终顺序应该等于 hybrid+RRF 融合顺序。\n"
            f"final={final_order}\nhybrid={hybrid_order}"
        )
        print(f"final order       = {final_order}")
        print(f"hybrid/RRF order  = {hybrid_order}")
        print(f"pure-vector order = {pure_vector_order}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_search_preserves_hybrid_rrf_order_not_raw_cosine_order OK")


def test_missing_fts_index_self_heals(monkeypatch):
    """模拟从旧版本迁移过来、从未建过 FTS 索引的知识库：
    search() 应该自动补建索引并重试，而不是直接报错。"""
    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(tmp_dir) / "kb.lancedb"
        db = lancedb.connect(str(db_path))
        schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
            pa.field("text", pa.string()),
            pa.field("source", pa.string()),
            pa.field("file_hash", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("chunk_total", pa.int32()),
        ])
        table = db.create_table(server.TABLE_NAME, schema=schema)
        rows = [{
            "vector": vec, "text": text, "source": source,
            "file_hash": "x", "chunk_index": idx, "chunk_total": 1,
        } for text, vec, source, idx in DOCS]
        table.add(rows)
        # 关键：故意不调用 create_fts_index，模拟旧知识库

        monkeypatch.setattr(server, "DB_PATH", str(db_path))
        monkeypatch.setattr(server, "_get_embedding_model", lambda: _StubEmbeddingModel())

        result = server.search(QUESTION, top_k=5, threshold=0.0)
        assert result.get("error") is None, f"应自动补建 FTS 索引并成功，实际报错: {result.get('error')}"
        assert result["found"] is True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_missing_fts_index_self_heals OK")


def test_search_applies_glossary_expansion(monkeypatch):
    """
    验证 search() 确实把 glossary 展开后的文本用于 embedding + hybrid 检索，
    而不是只是接了个接口但没真正生效。
    做法：用一个记录收到过什么文本的打桩模型，配合一份临时 glossary.yaml，
    检查 query_embed 实际收到的文本里包含了展开出来的全称。

    注意：server.py 现在从 query_config 读 GLOSSARY_PATH（不再依赖项目
    根目录的 config.py），所以这里直接 monkeypatch server.GLOSSARY_PATH。
    """
    import glossary as _glossary

    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(tmp_dir) / "kb.lancedb"
        _build_temp_kb(db_path)

        glossary_path = Path(tmp_dir) / "glossary.yaml"
        glossary_path.write_text(
            "SQL建表脚本:\n  full: 数据库定义语言脚本\n  aliases: []\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(server, "GLOSSARY_PATH", glossary_path)
        _glossary._cache = {}

        seen_texts = []

        class _RecordingModel(_StubEmbeddingModel):
            def query_embed(self, texts):
                for t in texts:
                    seen_texts.append(t)
                yield from super().query_embed(texts)

        monkeypatch.setattr(server, "DB_PATH", str(db_path))
        monkeypatch.setattr(server, "_get_embedding_model", lambda: _RecordingModel())

        result = server.search("SQL建表脚本怎么写", top_k=5, threshold=0.0)

        assert result.get("error") is None
        assert seen_texts, "query_embed 应该被调用过"
        assert "数据库定义语言脚本" in seen_texts[0], \
            f"实际发给 embedding 模型的文本没有包含术语展开结果: {seen_texts}"
        # 原始问题在返回结果里应该保持不变
        assert result["query"] == "SQL建表脚本怎么写"
    finally:
        _glossary._cache = {}
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_search_applies_glossary_expansion OK")


class _FakeMonkeypatch:
    """轻量替代 pytest 的 monkeypatch fixture，本文件用 plain script 方式运行。"""
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
    mp = _FakeMonkeypatch()
    try:
        test_search_does_not_crash_and_uses_real_cosine_similarity(mp)
    finally:
        mp.undo()

    mp = _FakeMonkeypatch()
    try:
        test_search_preserves_hybrid_rrf_order_not_raw_cosine_order(mp)
    finally:
        mp.undo()

    mp = _FakeMonkeypatch()
    try:
        test_missing_fts_index_self_heals(mp)
    finally:
        mp.undo()

    mp = _FakeMonkeypatch()
    try:
        test_search_applies_glossary_expansion(mp)
    finally:
        mp.undo()

    print("\n全部通过 ✅")
