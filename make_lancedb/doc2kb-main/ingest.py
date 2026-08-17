"""
doc2kb — 知识库入库模块
===========================
从 Markdown 文件构建 RAG 向量知识库（LanceDB）：
  1. 读取 .md 文件
  2. 按 Markdown 结构分块 (MarkdownTextSplitter)
  3. 用 fastembed 生成本地 CPU 嵌入向量
  4. 写入 LanceDB 向量库

支持增量更新：文件变更时删除旧向量、插入新向量。
"""

import os
import re
import threading
import traceback
from pathlib import Path
from typing import Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    DB_PATH, OUTPUT_MD_DIR, TABLE_NAME,
    EMBEDDING_MODEL, VECTOR_DIM, EMBEDDING_MAX_TOKENS,
    CHUNK_SIZE, CHUNK_OVERLAP,
    EMBED_BATCH_SIZE, DB_FLUSH_INTERVAL,
    CONVERT_WORKERS, CONVERT_TIMEOUT,
    CODE_CONFIG_EXTENSIONS, SOURCE_EXT_MARKER_PREFIX,
)
from validate import is_file_readable
from state import compute_sha256

# ============================================================
# 惰性加载（避免 import 时加载模型）
# ============================================================

_embed_model = None
_embed_model_lock = threading.Lock()
_db = None
_table = None
_lancedb_lock = threading.Lock()

_imported_lancedb = False
_lancedb_module = None
_pa_module = None

# ============================================================
# 超时放弃标记
# ============================================================
# ThreadPoolExecutor 无法真正取消一个已经在运行的任务：
# ingest_batch 里的 future.cancel() 对"已开始执行"的任务不起作用，
# 那个线程其实还在后台跑，跑完后仍可能把结果写进 LanceDB / 覆盖 state，
# 和后续重试/新一轮 build 产生竞争，写入过期或重复的数据。
# 这里用一个显式的"放弃名单"，让判定为超时的文件即使后台线程后来跑完，
# 也不再写库/不再当作正常结果处理。
_abandoned_paths: set = set()
_abandoned_lock = threading.Lock()


def mark_abandoned(rel_path: str):
    """将某个源文件标记为已放弃（超时），后续写入会被跳过。"""
    with _abandoned_lock:
        _abandoned_paths.add(rel_path)


def is_abandoned(rel_path: str) -> bool:
    with _abandoned_lock:
        return rel_path in _abandoned_paths


def _clear_abandoned(rel_path: str):
    with _abandoned_lock:
        _abandoned_paths.discard(rel_path)


def _get_embed_model():
    """
    获取（或初始化）fastembed 嵌入模型。

    max_length 使用模型真实的最大 token 长度（EMBEDDING_MAX_TOKENS），
    而不是 Markdown 分块用的字符数 CHUNK_SIZE —— 两者单位不同（token vs 字符），
    误用字符数只是让参数形同虚设：真正生效的截断长度始终是模型本身的位置编码上限。

    用双重检查锁定（double-checked locking）保证模型只被初始化一次：
    ingest_batch 是多线程并行跑的（CONVERT_WORKERS 个线程），如果不加锁，
    第一次调用时会有多个线程同时看到 _embed_model is None，各自触发一次
    TextEmbedding(...) 初始化——模型没缓存到本地时，就是好几个线程同时去
    下载同一个模型，白白浪费带宽、加重网络本就不稳时的下载失败概率。
    """
    global _embed_model
    if _embed_model is None:
        with _embed_model_lock:
            if _embed_model is None:  # 双重检查：拿到锁后可能已经被别的线程初始化好了
                from fastembed import TextEmbedding
                _embed_model = TextEmbedding(
                    model_name=EMBEDDING_MODEL,
                    max_length=EMBEDDING_MAX_TOKENS,
                    # 关掉 onnxruntime CPU 内存 arena：arena 默认只增不减，
                    # 会一直缓存"曾经用过的最大内存块"不释放，长时间批量跑
                    # 大量文件时内存会持续走高、不会随着处理小文件回落。
                    # 关掉后每次推理走普通 malloc/free，内存能正常释放，
                    # 代价是有一点点分配开销（对批量入库这种场景可忽略）。
                    enable_cpu_mem_arena=False,
                )
    return _embed_model


def _open_or_create_db():
    """
    打开（或创建）LanceDB 数据库和表。
    线程安全：使用全局锁防止并发创建。
    """
    global _db, _table, _lancedb_module, _pa_module

    with _lancedb_lock:
        if _db is None:
            import lancedb as _lancedb_module
            _db = _lancedb_module.connect(str(DB_PATH))

        if _table is None:
            import pyarrow as _pa_module
            schema = _pa_module.schema([
                _pa_module.field("vector", _pa_module.list_(_pa_module.float32(), VECTOR_DIM)),
                _pa_module.field("text", _pa_module.string()),
                _pa_module.field("source", _pa_module.string()),
                _pa_module.field("file_hash", _pa_module.string()),
                _pa_module.field("chunk_index", _pa_module.int32()),
                _pa_module.field("chunk_total", _pa_module.int32()),
                # "doc" | "sql" | "yaml" | "json" | "ini"，用于区分内容来源
                # （旧版本建的表没有这一列，写入时会自动降级不带这个字段，
                # 需要 --full 全量重建才能给存量数据补上）。
                _pa_module.field("doc_type", _pa_module.string()),
            ])

            try:
                _table = _db.open_table(TABLE_NAME)
            except Exception:
                _table = _db.create_table(TABLE_NAME, schema=schema)
                _table.create_fts_index("text", replace=True)

        return _table


def _table_has_column(table, column: str) -> bool:
    try:
        return column in table.schema.names
    except Exception:
        return False


def close_db():
    """关闭 LanceDB 连接（释放资源）。"""
    global _db, _table
    with _lancedb_lock:
        _table = None
        _db = None


# ============================================================
# 分块
# ============================================================

# ============================================================
# 分块
# ============================================================

def _detect_source_ext(content: str) -> Optional[str]:
    """检测 convert.py 写入的 source_ext 标记（若有），返回如 '.sql'。"""
    first_line = content.split("\n", 1)[0].strip()
    if first_line.startswith(SOURCE_EXT_MARKER_PREFIX):
        rest = first_line[len(SOURCE_EXT_MARKER_PREFIX):]
        ext = rest.split("-->", 1)[0].strip()
        if ext in CODE_CONFIG_EXTENSIONS:
            return ext
    return None


def _strip_marker_and_fence(content: str) -> str:
    """去掉 source_ext 标记行和外层 ``` 代码围栏，还原成原始文本内容。"""
    lines = content.split("\n")
    if lines and lines[0].strip().startswith(SOURCE_EXT_MARKER_PREFIX):
        lines = lines[1:]
    text = "\n".join(lines).strip("\n")
    text = text.strip()
    if text.startswith("```"):
        # 去掉第一行的 ```lang
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if text.endswith("```"):
        text = text.rsplit("\n", 1)[0] if "\n" in text else ""
    return text


def _group_pieces_by_size(pieces: List[str], max_size: int) -> List[str]:
    """
    把若干语义片段（SQL 语句/YAML 顶层 key/INI section）合并成不超过
    max_size 字符的 chunk：连续的小片段尽量合并到一起，减少"一条 SQL
    语句一个 chunk"这种过度碎片化；单个片段本身就超过 max_size 时
    （比如一张字段特别多的表），只能对它单独做无重叠的硬切分——
    语义边界已经找不到更小的单元了。
    """
    grouped: List[str] = []
    buf = ""
    for piece in pieces:
        piece = piece.strip("\n")
        if not piece.strip():
            continue
        candidate = f"{buf}\n\n{piece}" if buf else piece
        if len(candidate) <= max_size:
            buf = candidate
            continue
        if buf:
            grouped.append(buf)
            buf = ""
        if len(piece) <= max_size:
            buf = piece
        else:
            # 单个片段本身超限，硬切（已经是最小语义单元，没有更细的边界可切）
            for i in range(0, len(piece), max_size):
                grouped.append(piece[i:i + max_size])
    if buf:
        grouped.append(buf)
    return grouped


def _split_sql_statements(text: str) -> List[str]:
    """
    按语句边界切分 SQL：在单引号字符串/`--` 行注释/`/* */` 块注释之外
    遇到的 `;` 才算一条语句的结束，避免把字符串常量里的分号或者注释里的
    文字误判为语句边界。
    """
    statements = []
    buf = []
    i, n = 0, len(text)
    in_single_quote = False
    in_line_comment = False
    in_block_comment = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if in_single_quote:
            buf.append(ch)
            if ch == "'" and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single_quote = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            buf.append(ch)
            statements.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    if "".join(buf).strip():
        statements.append("".join(buf))

    return [s.strip() for s in statements if s.strip()]


_YAML_TOP_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+\s*:")


def _split_yaml_top_level(text: str) -> List[str]:
    """
    按顶层 key（行首不缩进、形如 `key:` 的行）切分 YAML，保留原始格式/注释。
    第一个顶层 key 之前的内容（文件头部注释/前言）并入第一个 key 的片段，
    不单独成一个孤立片段。
    """
    lines = text.split("\n")
    pieces: List[str] = []
    buf: List[str] = []
    started = False
    for line in lines:
        if _YAML_TOP_KEY_RE.match(line):
            if started:
                pieces.append("\n".join(buf))
                buf = [line]
            else:
                buf.append(line)
                started = True
        else:
            buf.append(line)
    if buf:
        pieces.append("\n".join(buf))
    return pieces if pieces else [text]


_INI_SECTION_RE = re.compile(r"^\s*\[.+\]\s*$")


def _split_ini_sections(text: str) -> List[str]:
    """按 `[section]` / `[[section]]` 切分 INI/conf/TOML。"""
    lines = text.split("\n")
    pieces, buf = [], []
    for line in lines:
        if _INI_SECTION_RE.match(line) and buf:
            pieces.append("\n".join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        pieces.append("\n".join(buf))
    return pieces if pieces else [text]


def _split_json_top_level(text: str) -> List[str]:
    """
    按顶层 key 切分 JSON：每个 key 单独 pretty-print 成一个片段。
    会丢失原始的排版/注释（JSON 本来就不支持注释），但保留了语义完整性——
    不会出现"半个 JSON 对象"这种解析不出来的碎片。
    解析失败时交给调用方按通用字符数分块兜底。
    """
    import json
    data = json.loads(text)
    if isinstance(data, dict) and data:
        return [
            json.dumps({k: v}, ensure_ascii=False, indent=2)
            for k, v in data.items()
        ]
    # 顶层是 list 或空 dict：没有天然的"顶层 key"可切，整体作为一块
    return [json.dumps(data, ensure_ascii=False, indent=2)]


def _chunk_code_config_content(raw_text: str, source_ext: str) -> List[str]:
    """按文件类型选择语义切分策略，失败时兜底为通用字符数切分。"""
    try:
        if source_ext == ".sql":
            pieces = _split_sql_statements(raw_text)
        elif source_ext in (".yaml", ".yml"):
            pieces = _split_yaml_top_level(raw_text)
        elif source_ext == ".json":
            pieces = _split_json_top_level(raw_text)
        elif source_ext in (".ini", ".conf", ".toml"):
            pieces = _split_ini_sections(raw_text)
        else:
            pieces = [raw_text]
    except Exception:
        # 解析失败（如 JSON 格式非法）：退回通用切分，不能因为一个文件
        # 解析不了就让整个 build 失败
        pieces = [raw_text]

    if not pieces:
        pieces = [raw_text]
    return _group_pieces_by_size(pieces, CHUNK_SIZE)


_DOC_TYPE_BY_EXT = {
    ".sql": "sql", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".ini": "ini", ".conf": "ini", ".toml": "toml",
}


def chunk_markdown_file(md_path: Path) -> List[dict]:
    """
    将 .md 文件分块。

    普通文档走 langchain 的 MarkdownTextSplitter（按字符数 + Markdown 结构切）；
    如果文件是 convert.py 转换来的 SQL/YAML/JSON/INI/conf/TOML（通过文件
    首行的 source_ext 标记识别），改用对应的语义切分策略：按 SQL 语句/
    YAML 顶层 key/INI section 切，而不是按固定字符数硬切，避免把一条
    CREATE TABLE 语句或一个配置块从中间切断。

    Returns
    -------
    List[dict]: [
        {
            "text": "块文本内容",
            "source": "相对路径",
            "file_hash": "sha256",
            "chunk_index": 0,
            "chunk_total": 10,
            "doc_type": "doc" | "sql" | "yaml" | "json" | "ini" | "toml",
        },
        ...
    ]
    失败时返回空列表。
    """
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return []

    source_ext = _detect_source_ext(content)

    if source_ext:
        raw_text = _strip_marker_and_fence(content)
        chunks = _chunk_code_config_content(raw_text, source_ext)
        doc_type = _DOC_TYPE_BY_EXT.get(source_ext, "doc")
    else:
        from langchain_text_splitters import MarkdownTextSplitter
        splitter = MarkdownTextSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
        )
        chunks = splitter.split_text(content)
        doc_type = "doc"

    rel_path = md_path.relative_to(OUTPUT_MD_DIR).as_posix()
    sha256 = compute_sha256(md_path)
    total = len(chunks)

    return [
        {
            "text": chunk,
            "source": rel_path,
            "file_hash": sha256,
            "chunk_index": i,
            "chunk_total": total,
            "doc_type": doc_type,
        }
        for i, chunk in enumerate(chunks)
    ]


# ============================================================
# 向量生成与入库
# ============================================================

def _delete_old_chunks(table, source_rel_path: str):
    """从 LanceDB 中删除指定源文件的所有旧分块（线程安全）。"""
    # 路径中如果含双引号（Linux 下合法文件名字符），不转义会拼出错误的
    # 过滤表达式，轻则删除失败，重则匹配到不该匹配的行——SQL 过滤字符串
    # 拼接必须转义引号。
    escaped = source_rel_path.replace('"', '""')
    with _lancedb_lock:
        try:
            table.delete(f'source = "{escaped}"')
        except Exception:
            pass  # 表可能是空的，忽略


def ingest_single_md(md_path: Path, source_rel_path: str,
                     file_sha256: str) -> dict:
    """
    将单个 .md 文件入库到 LanceDB。

    Parameters
    ----------
    md_path : Path
        .md 文件的绝对路径。
    source_rel_path : str
        源文件的相对路径（用于 LanceDB 索引删除/查询）。
    file_sha256 : str
        源文件的 SHA256（用于变更检测）。

    Returns
    -------
    dict: {
        "rel_path": source_rel_path,
        "status": "ok" | "empty" | "garbled" | "error",
        "chunks": int,
        "error": str | None,
    }
    """
    result = {
        "rel_path": source_rel_path,
        "status": "error",
        "chunks": 0,
        "error": None,
    }

    try:
        # 1. 检查文件是否存在且可读
        if not md_path.exists():
            result["status"] = "error"
            result["error"] = f"MD 文件不存在: {md_path}"
            return result

        if not is_file_readable(md_path):
            result["status"] = "garbled"
            result["error"] = "MD 文件内容疑似乱码"
            return result

        # 2. 分块
        chunks = chunk_markdown_file(md_path)
        if not chunks:
            result["status"] = "empty"
            result["error"] = "分块后内容为空"
            return result

        # 若主线程已判定该文件超时并放弃（见 ingest_batch），
        # 这个后台线程不再产生任何写入，避免和后续重试/新一轮 build 竞争写库。
        if is_abandoned(source_rel_path):
            result["status"] = "error"
            result["error"] = "任务已超时被放弃，本次结果不写入知识库（避免与后续重试竞争）"
            return result

        # 3. 打开 LanceDB
        table = _open_or_create_db()

        # 4. 删除该文件的旧向量（增量更新）
        _delete_old_chunks(table, source_rel_path)

        if is_abandoned(source_rel_path):
            result["status"] = "error"
            result["error"] = "任务已超时被放弃，本次结果不写入知识库（避免与后续重试竞争）"
            return result

        # 5. 生成 embedding 向量
        # 注意：这里必须用 passage_embed（文档侧），配合查询侧的 query_embed 使用，
        # 才能用上 BGE 系列"非对称检索"的指令前缀设计（query 和 passage 的编码方式不同）。
        # 两侧都用普通 embed() 会导致检索质量下降。
        model = _get_embed_model()
        texts = [c["text"] for c in chunks]

        # fastembed 返回的是生成器，分批消费
        all_vectors = []
        for batch_vecs in model.passage_embed(texts, batch_size=EMBED_BATCH_SIZE):
            all_vectors.append(batch_vecs)

        # 扁平化向量列表
        vectors = []
        for batch in all_vectors:
            vectors.extend(batch)

        # 6. 组装 LanceDB 记录
        import numpy as np
        import pyarrow as pa

        # 将所有向量转为 numpy float32 数组 (n_chunks, vector_dim)
        vec_array = np.array([np.asarray(v, dtype=np.float32) for v in vectors],
                            dtype=np.float32)

        # 构建 pyarrow FixedSizeListArray（LanceDB 要求）
        flat_vecs = pa.array(vec_array.ravel().tolist(), type=pa.float32())
        vectors_pa = pa.FixedSizeListArray.from_arrays(flat_vecs, VECTOR_DIM)

        # 构建各个字段的 pyarrow 数组
        texts_pa = pa.array([c["text"] for c in chunks], type=pa.string())
        sources_pa = pa.array([c["source"] for c in chunks], type=pa.string())
        hashes_pa = pa.array([file_sha256] * len(chunks), type=pa.string())
        indices_pa = pa.array([c["chunk_index"] for c in chunks], type=pa.int32())
        totals_pa = pa.array([c["chunk_total"] for c in chunks], type=pa.int32())
        doc_types_pa = pa.array([c.get("doc_type", "doc") for c in chunks], type=pa.string())

        # 组装成 pyarrow Table 并写入。
        # doc_type 是新加的列：旧版本建的表没有它，直接按新 schema 写入会报错，
        # 这里探测一下目标表是否已经有这一列，没有就不带这个字段写入
        # （已有数据/表结构不受影响，只是拿不到 doc_type 过滤能力，
        # 需要 `--full` 全量重建才能给存量数据补上）。
        include_doc_type = _table_has_column(table, "doc_type")
        fields = [
            pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
            pa.field("text", pa.string()),
            pa.field("source", pa.string()),
            pa.field("file_hash", pa.string()),
            pa.field("chunk_index", pa.int32()),
            pa.field("chunk_total", pa.int32()),
        ]
        data = {
            "vector": vectors_pa,
            "text": texts_pa,
            "source": sources_pa,
            "file_hash": hashes_pa,
            "chunk_index": indices_pa,
            "chunk_total": totals_pa,
        }
        if include_doc_type:
            fields.append(pa.field("doc_type", pa.string()))
            data["doc_type"] = doc_types_pa
        schema = pa.schema(fields)
        pa_table = pa.table(data, schema=schema)
        if is_abandoned(source_rel_path):
            result["status"] = "error"
            result["error"] = "任务已超时被放弃，本次结果不写入知识库（避免与后续重试竞争）"
            return result

        with _lancedb_lock:
            table.add(pa_table)

        result["status"] = "ok"
        result["chunks"] = len(chunks)

    except ImportError as e:
        missing_pkg = str(e).split(" ")[-1].replace("'", "")
        result["status"] = "error"
        result["error"] = f"缺少依赖库 {missing_pkg}"
        result["error"] += "\n请执行: uv pip install fastembed lancedb langchain-text-splitters pyarrow"
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    return result


def ingest_batch(md_file_map: List[Tuple[Path, str, str]],
                 max_workers: int = CONVERT_WORKERS,
                 timeout: int = CONVERT_TIMEOUT,
                 flush_interval: int = DB_FLUSH_INTERVAL,
                 progress_callback=None) -> List[dict]:
    """
    批量入库多个 .md 文件。

    Parameters
    ----------
    md_file_map : List[(md_abs_path, source_rel_path, file_sha256)]
        待入库的文件信息。
    max_workers : int
        并行线程数。
    timeout : int
        单文件超时秒数。
    flush_interval : int
        每处理 N 个文件后强制 flush 并释放内存。
    progress_callback : callable, optional
        每完成一个文件的回调。

    Returns
    -------
    List[dict]
    """
    results = []
    count_since_flush = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for md_path, src_rel, sha in md_file_map:
            future = pool.submit(ingest_single_md, md_path, src_rel, sha)
            futures[future] = (md_path, src_rel)

        try:
            for future in as_completed(futures):
                md_path, src_rel = futures[future]
                try:
                    result = future.result(timeout=timeout)
                except TimeoutError:
                    # ThreadPoolExecutor 无法真正打断一个已在运行的任务，
                    # future.cancel() 对它没有效果——线程可能仍在后台跑。
                    # 标记为"已放弃"，让它即使后来跑完也不再写库，避免和
                    # 下一次重试产生竞争（写入过期/重复数据）。
                    mark_abandoned(src_rel)
                    result = {
                        "rel_path": src_rel,
                        "status": "error",
                        "chunks": 0,
                        "error": f"入库超时（>{timeout}s），已跳过（后台线程可能仍在运行，其结果将被丢弃）",
                    }
                    future.cancel()
                results.append(result)
                count_since_flush += 1

                if progress_callback:
                    progress_callback(result)

                # 定期触发一次 Python 层 GC。
                # 注意：LanceDB 的 table.add() 每次调用就已经直接落盘，没有
                # 需要手动 flush 的写缓冲区；gc.collect() 也只能回收 Python
                # 侧有循环引用的对象，管不到 onnxruntime/LanceDB 这些原生
                # 扩展占用的内存——如果观察到内存持续走高，大概率是
                # onnxruntime 的 CPU 内存 arena 只增不减（见
                # _get_embed_model() 里 enable_cpu_mem_arena=False 的说明），
                # 不是这里能解决的，这里留着主要是清理一下 Python 侧的
                # 循环引用，聊胜于无。
                if count_since_flush >= flush_interval:
                    import gc
                    gc.collect()
                    count_since_flush = 0

        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # 排序保证结果稳定
    results.sort(key=lambda r: r["rel_path"])
    return results


# ============================================================
# 数据库管理
# ============================================================

def get_db_stats() -> dict:
    """
    获取知识库统计信息。
    """
    try:
        table = _open_or_create_db()
        count = table.count_rows()

        # 文件数：对 rel_path 列去重计数。只拉这一列（不拉 768 维向量），
        # 避免为了数个文件数就把整张表的向量都读出来。
        try:
            rel_paths = (
                table.to_lance()
                .to_table(columns=["rel_path"])
                .column("rel_path")
                .to_pylist()
            )
            file_count = len(set(rel_paths))
        except Exception:
            file_count = None

        return {
            "path": str(DB_PATH),
            "table": TABLE_NAME,
            "total_chunks": count,
            "file_count": file_count,
            "vector_dim": VECTOR_DIM,
            "model": EMBEDDING_MODEL,
        }
    except Exception as e:
        return {"error": f"无法读取知识库: {e}"}


def rebuild_table():
    """
    重建 LanceDB 表（清空所有数据）。
    用于 --full 全量重建场景。
    """
    close_db()
    import shutil
    db_path = Path(DB_PATH)
    if db_path.exists():
        shutil.rmtree(db_path)
    # 重新创建
    _open_or_create_db()
    close_db()
