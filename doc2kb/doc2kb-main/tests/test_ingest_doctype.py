"""
测试 ingest.py 的 LanceDB 写入路径：
1. 新建的表应该带 doc_type 列，且 SQL/YAML 等类型的 chunk 能正确写入并读出。
2. 旧版本的表（没有 doc_type 列）在升级后仍然能正常写入，只是不带这个新字段，
   不应该因为 schema 不匹配直接报错。
"""
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import lancedb
import pyarrow as pa

import ingest  # noqa: E402
import config  # noqa: E402


def _make_test_md(rel_dir_name: str, filename: str, content: str) -> Path:
    test_dir = ingest.OUTPUT_MD_DIR / rel_dir_name
    test_dir.mkdir(parents=True, exist_ok=True)
    md_path = test_dir / filename
    md_path.write_text(content, encoding="utf-8")
    return md_path


def test_new_table_gets_doc_type_column_and_correct_values():
    tmp_dir = tempfile.mkdtemp()
    md_path = None
    try:
        db_path = Path(tmp_dir) / "kb.lancedb"
        ingest.DB_PATH = db_path
        ingest.close_db()  # 确保没有复用之前测试留下的单例连接/表

        md_content = (
            f"{config.SOURCE_EXT_MARKER_PREFIX}.sql -->\n\n```sql\n"
            "-- 用户表建表脚本，记录系统用户基础信息\n"
            "CREATE TABLE t_user (\n"
            "    id INT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',\n"
            "    username VARCHAR(64) NOT NULL COMMENT '用户名',\n"
            "    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'\n"
            ");\n"
            "```\n"
        )
        md_path = _make_test_md("_test_doctype_new", "schema.md", md_content)
        rel_path = md_path.relative_to(ingest.OUTPUT_MD_DIR).as_posix()

        # 打桩 embedding：不下载真实模型，返回固定向量
        import numpy as np

        class _StubModel:
            def passage_embed(self, texts, batch_size=32):
                yield [np.zeros(config.VECTOR_DIM, dtype=np.float32) for _ in texts]

        ingest._embed_model = _StubModel()

        result = ingest.ingest_single_md(md_path, rel_path, "fakehash")
        assert result["status"] == "ok", result
        assert result["chunks"] >= 1

        db = lancedb.connect(str(db_path))
        table = db.open_table(config.TABLE_NAME)
        assert "doc_type" in table.schema.names
        rows = table.to_pandas()
        matched = rows[rows["source"] == rel_path]
        assert len(matched) >= 1
        assert (matched["doc_type"] == "sql").all()
    finally:
        ingest.close_db()
        ingest._embed_model = None
        if md_path:
            md_path.unlink(missing_ok=True)
            md_path.parent.rmdir()
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_new_table_gets_doc_type_column_and_correct_values OK")


def test_writing_to_legacy_table_without_doc_type_does_not_crash():
    """模拟从旧版本迁移过来、还没有 doc_type 列的表，验证升级后写入不报错。"""
    tmp_dir = tempfile.mkdtemp()
    md_path = None
    try:
        db_path = Path(tmp_dir) / "legacy.lancedb"

        # 手工建一张"旧版本 schema"的表（没有 doc_type 列）
        db = lancedb.connect(str(db_path))
        legacy_schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), config.VECTOR_DIM)),
            pa.field("text", pa.string()),
            pa.field("source", pa.string()),
            pa.field("file_hash", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("chunk_total", pa.int32()),
        ])
        db.create_table(config.TABLE_NAME, schema=legacy_schema)

        ingest.DB_PATH = db_path
        ingest.close_db()

        md_path = _make_test_md("_test_doctype_legacy", "doc.md",
                                 "# 标题\n\n普通文档内容，用来测试旧表兼容性。" * 5)
        rel_path = md_path.relative_to(ingest.OUTPUT_MD_DIR).as_posix()

        import numpy as np

        class _StubModel:
            def passage_embed(self, texts, batch_size=32):
                yield [np.zeros(config.VECTOR_DIM, dtype=np.float32) for _ in texts]

        ingest._embed_model = _StubModel()

        result = ingest.ingest_single_md(md_path, rel_path, "fakehash2")
        assert result["status"] == "ok", f"旧表写入不应该报错，实际: {result}"
        assert result["chunks"] >= 1

        table = db.open_table(config.TABLE_NAME)
        assert "doc_type" not in table.schema.names, "旧表 schema 不应该被意外改变"
        rows = table.to_pandas()
        assert len(rows[rows["source"] == rel_path]) >= 1
    finally:
        ingest.close_db()
        ingest._embed_model = None
        if md_path:
            md_path.unlink(missing_ok=True)
            md_path.parent.rmdir()
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("test_writing_to_legacy_table_without_doc_type_does_not_crash OK")


if __name__ == "__main__":
    test_new_table_gets_doc_type_column_and_correct_values()
    test_writing_to_legacy_table_without_doc_type_does_not_crash()
    print("\n全部通过 ✅")
