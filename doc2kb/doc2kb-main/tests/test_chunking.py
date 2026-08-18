"""
测试 ingest.py 里 SQL/YAML/JSON/INI 的语义切分逻辑。
纯逻辑测试，不需要真实 LanceDB/embedding。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import ingest  # noqa: E402


SQL_SAMPLE = """-- 版本: 1.0
-- 作者: zhangsan
CREATE TABLE users (
    id INT PRIMARY KEY,
    name VARCHAR(50) DEFAULT 'a;b'  -- 字符串里的分号不该被当成语句边界
);

CREATE TABLE orders (
    id INT PRIMARY KEY,
    user_id INT,
    /* 块注释里也有一个 ; 分号 */
    total DECIMAL(10,2)
);

CREATE INDEX idx_orders_user ON orders(user_id);
"""


def test_split_sql_statements_respects_strings_and_comments():
    stmts = ingest._split_sql_statements(SQL_SAMPLE)
    assert len(stmts) == 3, f"应该切出 3 条语句，实际 {len(stmts)}: {stmts}"
    assert "CREATE TABLE users" in stmts[0]
    assert "DEFAULT 'a;b'" in stmts[0]  # 字符串内容完整保留，没被从中间切断
    assert "CREATE TABLE orders" in stmts[1]
    assert "CREATE INDEX idx_orders_user" in stmts[2]
    print("test_split_sql_statements_respects_strings_and_comments OK")


def test_split_sql_statements_no_trailing_semicolon():
    # 最后一条语句没有分号结尾，也应该被当作一条语句收进来，不能丢
    stmts = ingest._split_sql_statements("CREATE TABLE a (id INT);\nCREATE TABLE b (id INT)")
    assert len(stmts) == 2
    assert "CREATE TABLE b" in stmts[1]
    print("test_split_sql_statements_no_trailing_semicolon OK")


YAML_SAMPLE = """# 全局配置
version: 1.0
database:
  host: localhost
  port: 5432
  # 这是一段注释，应该保留在原片段里
  credentials:
    user: admin
cache:
  ttl: 300
  backend: redis
"""


def test_split_yaml_top_level_keeps_nested_content_together():
    pieces = ingest._split_yaml_top_level(YAML_SAMPLE)
    # 顶层 key 数量：version / database / cache = 3 个片段
    # （文件开头的注释并入第一个 key 的片段里，不单独成孤立片段）
    assert len(pieces) == 3, f"应该切出 3 个顶层片段，实际 {len(pieces)}: {pieces}"
    assert "# 全局配置" in pieces[0] and "version: 1.0" in pieces[0]
    db_piece = [p for p in pieces if p.strip().startswith("database:")][0]
    assert "credentials" in db_piece and "user: admin" in db_piece, \
        "database 顶层 key 下的嵌套内容应该完整保留在同一个片段里，不能被切断"
    assert "这是一段注释" in db_piece, "YAML 注释应该被保留（不同于走文档清洗那条路径）"
    print("test_split_yaml_top_level_keeps_nested_content_together OK")


JSON_SAMPLE = """{
  "app_name": "doc2kb",
  "database": {"host": "localhost", "port": 5432},
  "features": ["search", "ingest", "convert"]
}"""


def test_split_json_top_level_one_chunk_per_key():
    pieces = ingest._split_json_top_level(JSON_SAMPLE)
    assert len(pieces) == 3
    import json
    parsed = [json.loads(p) for p in pieces]
    keys = [list(p.keys())[0] for p in parsed]
    assert set(keys) == {"app_name", "database", "features"}
    db_chunk = [p for p in parsed if "database" in p][0]
    assert db_chunk["database"]["port"] == 5432, "嵌套结构应该完整保留在对应 key 的片段里"
    print("test_split_json_top_level_one_chunk_per_key OK")


def test_split_json_invalid_falls_back_gracefully():
    # 不是合法 JSON：_chunk_code_config_content 应该兜底而不是抛异常炸整个 build
    chunks = ingest._chunk_code_config_content("{not valid json,,,", ".json")
    assert len(chunks) >= 1
    assert "not valid json" in chunks[0]
    print("test_split_json_invalid_falls_back_gracefully OK")


INI_SAMPLE = """[global]
timeout = 30

[database]
host = localhost
port = 5432

[cache]
backend = redis
"""


def test_split_ini_sections():
    pieces = ingest._split_ini_sections(INI_SAMPLE)
    assert len(pieces) == 3
    assert pieces[0].strip().startswith("[global]")
    assert pieces[1].strip().startswith("[database]")
    assert "port = 5432" in pieces[1]
    print("test_split_ini_sections OK")


def test_group_pieces_by_size_merges_small_pieces():
    pieces = ["a" * 10, "b" * 10, "c" * 10]
    grouped = ingest._group_pieces_by_size(pieces, max_size=100)
    assert len(grouped) == 1, "小片段应该被合并成一个 chunk"
    assert "a" * 10 in grouped[0] and "b" * 10 in grouped[0] and "c" * 10 in grouped[0]
    print("test_group_pieces_by_size_merges_small_pieces OK")


def test_group_pieces_by_size_splits_oversized_single_piece():
    huge = "x" * 250
    grouped = ingest._group_pieces_by_size([huge], max_size=100)
    assert len(grouped) == 3, f"250 字符超限片段按 100 硬切应该是 3 块，实际 {len(grouped)}"
    assert "".join(grouped) == huge
    print("test_group_pieces_by_size_splits_oversized_single_piece OK")


def test_detect_source_ext_and_strip_marker():
    from config import SOURCE_EXT_MARKER_PREFIX
    content = f"{SOURCE_EXT_MARKER_PREFIX}.sql -->\n\n```sql\nSELECT 1;\n```\n"
    ext = ingest._detect_source_ext(content)
    assert ext == ".sql"
    stripped = ingest._strip_marker_and_fence(content)
    assert stripped.strip() == "SELECT 1;"
    print("test_detect_source_ext_and_strip_marker OK")


def test_detect_source_ext_returns_none_for_normal_markdown():
    content = "# 标题\n\n普通文档内容"
    assert ingest._detect_source_ext(content) is None
    print("test_detect_source_ext_returns_none_for_normal_markdown OK")


def test_chunk_markdown_file_end_to_end_for_sql(tmp_path=None):
    """
    端到端：模拟 convert.py 产出的带 source_ext 标记的 .md 文件，
    验证 chunk_markdown_file() 能正确识别、语义切分、打上 doc_type。
    这里内容较短，3 条语句会被 _group_pieces_by_size 合并成 1 个 chunk
    （这正是分组逻辑该做的事——避免"一条语句一个 chunk"的过度碎片化），
    所以这里验证的重点是 doc_type 和内容完整性，而不是 chunk 数量。
    """
    from config import SOURCE_EXT_MARKER_PREFIX

    test_dir = ingest.OUTPUT_MD_DIR / "_test_chunking"
    test_dir.mkdir(parents=True, exist_ok=True)
    md_path = test_dir / "schema.md"
    md_content = (
        f"{SOURCE_EXT_MARKER_PREFIX}.sql -->\n\n```sql\n"
        f"{SQL_SAMPLE}\n```\n"
    )
    md_path.write_text(md_content, encoding="utf-8")
    try:
        chunks = ingest.chunk_markdown_file(md_path)
        assert len(chunks) >= 1
        assert all(c["doc_type"] == "sql" for c in chunks)
        assert all(c["source"] == "_test_chunking/schema.md" for c in chunks)
        full_text = "\n".join(c["text"] for c in chunks)
        assert "CREATE TABLE users" in full_text
        assert "CREATE TABLE orders" in full_text
        assert "CREATE INDEX idx_orders_user" in full_text
        assert "```" not in full_text, "应该已经去掉了外层 fence，不然会把 ``` 也当成内容切进去"
    finally:
        md_path.unlink(missing_ok=True)
        test_dir.rmdir()
    print("test_chunk_markdown_file_end_to_end_for_sql OK")


def test_chunk_markdown_file_normal_doc_gets_doc_type_doc():
    test_dir = ingest.OUTPUT_MD_DIR / "_test_chunking_doc"
    test_dir.mkdir(parents=True, exist_ok=True)
    md_path = test_dir / "normal.md"
    md_path.write_text("# 标题\n\n这是普通文档内容。" * 10, encoding="utf-8")
    try:
        chunks = ingest.chunk_markdown_file(md_path)
        assert len(chunks) >= 1
        assert all(c["doc_type"] == "doc" for c in chunks)
    finally:
        md_path.unlink(missing_ok=True)
        test_dir.rmdir()
    print("test_chunk_markdown_file_normal_doc_gets_doc_type_doc OK")


if __name__ == "__main__":
    test_split_sql_statements_respects_strings_and_comments()
    test_split_sql_statements_no_trailing_semicolon()
    test_split_yaml_top_level_keeps_nested_content_together()
    test_split_json_top_level_one_chunk_per_key()
    test_split_json_invalid_falls_back_gracefully()
    test_split_ini_sections()
    test_group_pieces_by_size_merges_small_pieces()
    test_group_pieces_by_size_splits_oversized_single_piece()
    test_detect_source_ext_and_strip_marker()
    test_detect_source_ext_returns_none_for_normal_markdown()
    test_chunk_markdown_file_end_to_end_for_sql()
    test_chunk_markdown_file_normal_doc_gets_doc_type_doc()
    print("\n全部通过 ✅")
