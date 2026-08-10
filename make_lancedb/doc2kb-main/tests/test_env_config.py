"""
测试 .env 配置加载：

1. config.py（构建端）能正确从项目根目录的 .env 读取配置、类型转换正确、
   没设置的变量会用默认值兜底。
2. query_reference/query_config.py（查询端）读的是自己目录下的 .env，
   和构建端完全独立——改一边的 .env 不会影响另一边。
3. 两边即使同时存在，变量名也不会互相冲突/覆盖。

因为 config.py / query_config.py 在 import 时就执行了 load_dotenv()，
这里用子进程分别跑，而不是在同一个进程里重复 import（模块只会被
import 一次，重复 import 不会重新读 .env，没法在同一进程里干净地
测试"改了 .env 之后重新加载"这件事）。
"""
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _run_python(code: str, cwd: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"子进程执行失败:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}")
    return result.stdout


def test_root_config_reads_env_file():
    with tempfile.TemporaryDirectory() as td:
        work_dir = Path(td)
        # 把 doc2kb 的 .py 文件复制一份到临时目录，避免污染真实项目里的文件
        for f in ["config.py", "glossary.py"]:
            (work_dir / f).write_text((REPO_ROOT / f).read_text(encoding="utf-8"), encoding="utf-8")

        (work_dir / ".env").write_text(
            "DOC2KB_CHUNK_SIZE=999\n"
            "DOC2KB_CONVERT_WORKERS=7\n"
            "DOC2KB_COLOR_OUTPUT=false\n"
            "DOC2KB_TABLE_NAME=my_custom_table\n",
            encoding="utf-8",
        )

        code = textwrap.dedent("""
            import config
            print("CHUNK_SIZE=", config.CHUNK_SIZE)
            print("CONVERT_WORKERS=", config.CONVERT_WORKERS)
            print("COLOR_OUTPUT=", config.COLOR_OUTPUT)
            print("TABLE_NAME=", config.TABLE_NAME)
        """)
        out = _run_python(code, cwd=work_dir)
        assert "CHUNK_SIZE= 999" in out, out
        assert "CONVERT_WORKERS= 7" in out, out
        assert "COLOR_OUTPUT= False" in out, out
        assert "TABLE_NAME= my_custom_table" in out, out
    print("test_root_config_reads_env_file OK")


def test_root_config_falls_back_to_defaults_without_env_file():
    with tempfile.TemporaryDirectory() as td:
        work_dir = Path(td)
        for f in ["config.py", "glossary.py"]:
            (work_dir / f).write_text((REPO_ROOT / f).read_text(encoding="utf-8"), encoding="utf-8")
        # 故意不写 .env 文件

        code = "import config; print('CHUNK_SIZE=', config.CHUNK_SIZE); print('TABLE_NAME=', config.TABLE_NAME)"
        out = _run_python(code, cwd=work_dir)
        assert "CHUNK_SIZE= 1200" in out, out
        assert "TABLE_NAME= docs" in out, out
    print("test_root_config_falls_back_to_defaults_without_env_file OK")


def test_query_config_is_independent_from_root_config():
    """
    query_config.py 读的是 query_reference/.env，跟项目根目录的 .env
    是两份独立文件——根目录 .env 里的变量名（DOC2KB_TABLE_NAME）和
    查询端的变量名（DOC2KB_QUERY_TABLE_NAME）故意不一样，用不同的值
    验证两边互不覆盖。
    """
    with tempfile.TemporaryDirectory() as td:
        work_dir = Path(td)
        query_dir = work_dir / "query_reference"
        query_dir.mkdir()

        (work_dir / "config.py").write_text((REPO_ROOT / "config.py").read_text(encoding="utf-8"), encoding="utf-8")
        (work_dir / "glossary.py").write_text((REPO_ROOT / "glossary.py").read_text(encoding="utf-8"), encoding="utf-8")
        (query_dir / "query_config.py").write_text(
            (REPO_ROOT / "query_reference" / "query_config.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        # 根目录 .env：TABLE_NAME 设成 "build_side_table"
        (work_dir / ".env").write_text("DOC2KB_TABLE_NAME=build_side_table\n", encoding="utf-8")
        # 查询端自己的 .env：TABLE_NAME 设成完全不同的值
        (query_dir / ".env").write_text(
            "DOC2KB_QUERY_TABLE_NAME=query_side_table\n"
            "DOC2KB_QUERY_EMBEDDING_MODEL=jinaai/jina-embeddings-v2-base-zh\n",
            encoding="utf-8",
        )

        code = textwrap.dedent("""
            import sys
            sys.path.insert(0, "query_reference")
            import config
            import query_config
            print("BUILD_TABLE_NAME=", config.TABLE_NAME)
            print("QUERY_TABLE_NAME=", query_config.TABLE_NAME)
            print("QUERY_EMBEDDING_MODEL=", query_config.EMBEDDING_MODEL)
            print("BUILD_EMBEDDING_MODEL=", config.EMBEDDING_MODEL)
        """)
        out = _run_python(code, cwd=work_dir)
        assert "BUILD_TABLE_NAME= build_side_table" in out, out
        assert "QUERY_TABLE_NAME= query_side_table" in out, out
        assert "QUERY_EMBEDDING_MODEL= jinaai/jina-embeddings-v2-base-zh" in out, out
        # 构建端的默认模型不应该被查询端的 .env 影响到
        assert "BUILD_EMBEDDING_MODEL= BAAI/bge-small-zh-v1.5" in out, out
    print("test_query_config_is_independent_from_root_config OK")


if __name__ == "__main__":
    test_root_config_reads_env_file()
    test_root_config_falls_back_to_defaults_without_env_file()
    test_query_config_is_independent_from_root_config()
    print("\n全部通过 ✅")
