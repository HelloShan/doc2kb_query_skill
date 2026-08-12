#!/usr/bin/env python3
"""
doc2kb 知识库查询 —— 自包含 Skill 脚本
========================================
  - 常驻 HTTP server（模型只加载一次）
  - 自动拉起（首次查询检测到没有 server 时在后台启动）
  - hybrid 检索（向量 + BM25 + RRF 融合）
  - 访问口令鉴权
  - 术语表展开
  - 向量维度运行时自检

完全独立，不依赖 doc2kb 项目的任何路径或模块。
配置通过本文件同目录下的 .env 读取（参考 .env.example）。

用法:
  # 单题查询（自动拉起常驻 server，后续查询复用同一进程）
  python query.py --question "你的问题"
  python query.py --question "你的问题" --format context

  # 批量查询
  python query.py --batch '[{"id":"1","question":"问题A"},{"id":"2","question":"问题B"}]'

  # 手动启动常驻服务（通常不需要，CLI 调用会自动拉起）
  python query.py --server
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# ============================================================
# 配置：从本文件同目录下的 .env 读取，不依赖项目根目录
# ============================================================

_SKILL_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(_SKILL_DIR / ".env")
except ImportError:
    # dotenv 未安装时降级：手动解析 .env 文件
    _env_file = _SKILL_DIR / ".env"
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name, "").strip()
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name, "").strip()
    try:
        return float(val) if val else default
    except ValueError:
        return default


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (_SKILL_DIR / p)


# ============================================================
# 必填知识库身份参数（移除默认值，为空时直接提示并退出）
# ============================================================
_raw_db_path      = os.environ.get("DOC2KB_QUERY_DB_PATH", "").strip()
_raw_table_name   = os.environ.get("DOC2KB_QUERY_TABLE_NAME", "").strip()
_raw_model        = os.environ.get("DOC2KB_QUERY_EMBEDDING_MODEL", "").strip()
_raw_max_tokens   = os.environ.get("DOC2KB_QUERY_EMBEDDING_MAX_TOKENS", "").strip()
_raw_glossary     = os.environ.get("DOC2KB_QUERY_GLOSSARY_PATH", "").strip()

_missing_configs = []
if not _raw_db_path:     _missing_configs.append("DOC2KB_QUERY_DB_PATH")
if not _raw_table_name:  _missing_configs.append("DOC2KB_QUERY_TABLE_NAME")
if not _raw_model:       _missing_configs.append("DOC2KB_QUERY_EMBEDDING_MODEL")
if not _raw_max_tokens:  _missing_configs.append("DOC2KB_QUERY_EMBEDDING_MAX_TOKENS")
if not _raw_glossary:    _missing_configs.append("DOC2KB_QUERY_GLOSSARY_PATH")

if _missing_configs:
    print("=====================================================", file=sys.stderr)
    print("[错误] 程序启动失败！检测到以下必要配置为空，请检查环境变量或 .env 设置：", file=sys.stderr)
    for m in _missing_configs:
        print(f" -> 缺少配置: {m}", file=sys.stderr)
    print("=====================================================", file=sys.stderr)
    sys.exit(1)

try:
    EMBEDDING_MAX_TOKENS = int(_raw_max_tokens)
except ValueError:
    print(f"[错误] DOC2KB_QUERY_EMBEDDING_MAX_TOKENS 必须是整数格式，当前值: {_raw_max_tokens}", file=sys.stderr)
    sys.exit(1)

DB_PATH       = _resolve_path(_raw_db_path)
TABLE_NAME    = _raw_table_name
EMBEDDING_MODEL = _raw_model
GLOSSARY_PATH = _resolve_path(_raw_glossary)


# ============================================================
# 可选/其它检索参数与服务监听配置
# ============================================================
TOP_K               = _env_int("DOC2KB_QUERY_TOP_K",              5)
SIMILARITY_THRESHOLD = _env_float("DOC2KB_QUERY_SIMILARITY_THRESHOLD", 0.5)

HOST         = os.environ.get("DOC2KB_QUERY_HOST",                 "127.0.0.1")
PORT         = _env_int("DOC2KB_QUERY_PORT",                       8788)
PORT_SCAN_RANGE = _env_int("DOC2KB_QUERY_PORT_SCAN_RANGE",         10)
IDLE_TIMEOUT = _env_int("DOC2KB_QUERY_IDLE_TIMEOUT",               3600)
SERVER_START_TIMEOUT = _env_int("DOC2KB_QUERY_SERVER_START_TIMEOUT", 60)
PID_FILE     = os.environ.get("DOC2KB_QUERY_PID_FILE", "")
QUERY_AUTH_TOKEN = os.environ.get("DOC2KB_QUERY_AUTH_TOKEN", "").strip()


def _db_identity() -> str:
    return str(DB_PATH.resolve())


def _pid_file_for_port(port: int) -> Path:
    if PID_FILE:
        base = Path(PID_FILE)
        return base.with_name(f"{base.stem}_{port}{base.suffix or '.pid'}")
    return _SKILL_DIR / f".server_{port}.pid"


# ============================================================
# 术语表展开（内嵌，不依赖外部 glossary.py）
# ============================================================

_glossary_cache: dict = {}
_glossary_lock  = threading.Lock()


def _load_glossary() -> dict:
    path = GLOSSARY_PATH
    key  = str(path.resolve())
    with _glossary_lock:
        if not path.exists():
            return {}
        mtime = path.stat().st_mtime
        cached = _glossary_cache.get(key)
        if cached and cached[1] == mtime:
            return cached[0]
        try:
            import yaml
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                return {}
            parsed: dict = {}
            for term, info in raw.items():
                if not isinstance(term, str):
                    continue
                if isinstance(info, dict):
                    full    = info.get("full", "")
                    aliases = info.get("aliases", []) or []
                elif isinstance(info, str):
                    full, aliases = info, []
                else:
                    continue
                parsed[term] = {
                    "full":    full,
                    "aliases": [a for a in aliases if isinstance(a, str)],
                }
            _glossary_cache[key] = (parsed, mtime)
            return parsed
        except Exception:
            return {}


def _expand_query(question: str) -> str:
    glossary = _load_glossary()
    if not glossary:
        return question
    extra: list[str] = []
    seen:  set[str]  = set()
    for term, info in glossary.items():
        pat = re.compile(
            rf"\b{re.escape(term)}\b" if term.isascii() else re.escape(term),
            re.IGNORECASE,
        )
        if not pat.search(question):
            continue
        for candidate in [info.get("full", "")] + info.get("aliases", []):
            candidate = candidate.strip()
            if candidate and candidate not in seen and candidate.lower() != term.lower():
                seen.add(candidate)
                extra.append(candidate)
    return (question + " " + " ".join(extra)) if extra else question


# ============================================================
# Embedding 模型（单例 + 双重检查锁定 + 下载提示）
# ============================================================

_embedding_model      = None
_embedding_model_lock = threading.Lock()


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        with _embedding_model_lock:
            if _embedding_model is None:
                print("=====================================================", file=sys.stderr)
                print(f"[提示] 正在准备加载 Embedding 模型: [{EMBEDDING_MODEL}]", file=sys.stderr)
                print(f"[提示] 若为首次运行，系统将自动从 HuggingFace 自动下载模型权重。", file=sys.stderr)
                print(f"[提示] 下载过程视网络情况可能需要几分钟时间，请耐心等待，不要关闭程序...", file=sys.stderr)
                print("=====================================================", file=sys.stderr)
                
                from fastembed import TextEmbedding
                _embedding_model = TextEmbedding(
                    model_name=EMBEDDING_MODEL,
                    max_length=EMBEDDING_MAX_TOKENS,
                    enable_cpu_mem_arena=False,   # 避免长驻进程内存只增不减
                )
                
                print(f"[提示] 模型 [{EMBEDDING_MODEL}] 加载完毕！", file=sys.stderr)
    return _embedding_model


def _get_query_vector(model, question: str) -> list:
    if hasattr(model, "query_embed"):
        vec = list(model.query_embed([question]))[0]
    else:
        vec = list(model.embed([question], batch_size=1))[0]
    return vec.tolist() if hasattr(vec, "tolist") else list(vec)


# ============================================================
# 向量维度运行时自检（防止构建/查询模型配置不一致）
# ============================================================

_dim_checked = False


def _validate_vector_dim(table):
    global _dim_checked
    if _dim_checked:
        return
    try:
        table_dim = table.schema.field("vector").type.list_size
    except Exception:
        _dim_checked = True
        return

    model     = _get_embedding_model()
    probe_vec = list(
        model.query_embed(["dim_check"]) if hasattr(model, "query_embed")
        else model.embed(["dim_check"], batch_size=1)
    )[0]
    probe_dim = len(probe_vec)

    if probe_dim != table_dim:
        raise ValueError(
            f"维度不匹配：当前模型 {EMBEDDING_MODEL!r} 输出 {probe_dim} 维，"
            f"但知识库里存的向量是 {table_dim} 维。"
            f"请检查 .env 里的 DOC2KB_QUERY_EMBEDDING_MODEL 是否和构建时一致，"
            f"同时确认 DOC2KB_QUERY_EMBEDDING_MAX_TOKENS 也对应修改。"
        )
    _dim_checked = True


# ============================================================
# LanceDB 连接（惰性单例）
# ============================================================

_db    = None
_table = None
_db_lock = threading.Lock()


def _get_db_and_table():
    global _db, _table
    if _table is not None:
        return _db, _table
    with _db_lock:
        if _table is not None:
            return _db, _table
        db_path = str(DB_PATH)
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"知识库路径不存在: {db_path}\n"
                rf"请先在 doc2kb_query_skill\make_lancedb\doc2kb-main 项目里运行: python doc_pipeline.py build"
            )
        import lancedb
        _db    = lancedb.connect(db_path)
        _table = _db.open_table(TABLE_NAME)
        _validate_vector_dim(_table)
    return _db, _table


# ============================================================
# 核心 hybrid 检索
# ============================================================

_FTS_MISSING = ("inverted index", "full text search")


def _run_hybrid_search(table, query_vec: list, text: str, limit: int) -> list:
    from lancedb.rerankers import RRFReranker

    def _do():
        return (
            table.search(query_type="hybrid")
            .vector(query_vec)
            .text(text)
            .rerank(reranker=RRFReranker())
            .limit(limit)
            .to_list()
        )

    try:
        return _do()
    except Exception as e:
        if any(m in str(e).lower() for m in _FTS_MISSING):
            table.create_fts_index("text", replace=True)
            return _do()
        raise


def _do_search(question: str, search_text: str, query_vec: list,
                top_k: int, threshold: float) -> dict:
    _, table = _get_db_and_table()
    fetch_limit = max(top_k * 3, 10)
    try:
        hybrid_raw = _run_hybrid_search(table, query_vec, search_text, fetch_limit)
    except Exception as e:
        return {"found": False, "query": question, "results": [], "hits": 0, "error": f"检索失败: {e}"}

    try:
        vector_raw = table.search(query_vec).metric("cosine").limit(max(top_k * 4, 20)).to_list()
    except Exception:
        vector_raw = []

    sim_lookup: dict = {}
    for r in vector_raw:
        key = (r.get("source", ""), r.get("chunk_index"))
        d   = r.get("_distance")
        if d is not None:
            sim_lookup[key] = round(1.0 - d, 4)

    filtered = []
    for r in hybrid_raw:
        key = (r.get("source", ""), r.get("chunk_index"))
        sim = sim_lookup.get(key)
        if sim is None:
            matched_by = "keyword" 
        else:
            matched_by = "hybrid"
            if sim < threshold:
                continue
        filtered.append({
            "text":       r.get("text", "").strip(),
            "source":     r.get("source", ""),
            "file_name":  os.path.basename(r.get("source", "未知")),
            "similarity": sim,
            "matched_by": matched_by,
            "doc_type":   r.get("doc_type", "doc"),
        })
    filtered = filtered[:top_k]

    numeric_sims = [r["similarity"] for r in filtered if r["similarity"] is not None]
    if not filtered:
        best, confidence = 0.0, "low"
    elif not numeric_sims:
        best, confidence = None, "medium"
    else:
        best       = numeric_sims[0]
        confidence = "high" if best >= 0.75 else "medium" if best >= 0.55 else "low"

    result: dict = {
        "found":            len(filtered) > 0,
        "query":            question,
        "results":          filtered,
        "hits":             len(filtered),
        "best_similarity":  round(best, 4) if isinstance(best, float) else best,
        "confidence":       confidence,
    }
    if search_text != question:
        result["expanded_query"] = search_text
    return result


def search(
    question: str,
    top_k: int = TOP_K,
    threshold: float = SIMILARITY_THRESHOLD,
) -> dict:
    _touch_activity()
    try:
        _get_db_and_table()
        model = _get_embedding_model()
    except Exception as e:
        return {"found": False, "query": question, "results": [], "hits": 0, "error": str(e)}

    try:
        search_text = _expand_query(question)
    except Exception:
        search_text = question

    try:
        query_vec = _get_query_vector(model, search_text)
    except Exception as e:
        return {"found": False, "query": question, "results": [], "hits": 0, "error": f"向量化失败: {e}"}

    return _do_search(question, search_text, query_vec, top_k, threshold)


def search_batch(items: list[dict], top_k: int = TOP_K, threshold: float = SIMILARITY_THRESHOLD) -> list[dict]:
    _touch_activity()
    out: list[dict] = [None] * len(items)  # type: ignore[list-item]
    valid: list[tuple[int, str, str]] = []  

    for i, item in enumerate(items):
        qid      = str(item.get("id", ""))
        question = str(item.get("question", "")).strip()
        if not question:
            out[i] = {"id": qid, "error": "empty question", "found": False, "results": [], "hits": 0}
        else:
            valid.append((i, qid, question))

    if not valid:
        return out  # type: ignore[return-value]

    try:
        _get_db_and_table()
        model = _get_embedding_model()
    except Exception as e:
        err = {"error": str(e), "found": False, "results": [], "hits": 0}
        for i, qid, question in valid:
            out[i] = {"id": qid, "query": question, **err}
        return out  # type: ignore[return-value]

    search_texts = []
    for _, _, question in valid:
        try:
            search_texts.append(_expand_query(question))
        except Exception:
            search_texts.append(question)

    try:
        if hasattr(model, "query_embed"):
            vecs = list(model.query_embed(search_texts))
        else:
            vecs = list(model.embed(search_texts, batch_size=min(len(search_texts), 32)))
        vecs = [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]
    except Exception as e:
        err_msg = f"向量化失败: {e}"
        for (i, qid, question), search_text in zip(valid, search_texts):
            out[i] = {"id": qid, "query": question, "error": err_msg, "found": False, "results": [], "hits": 0}
        return out  # type: ignore[return-value]

    for (i, qid, question), search_text, vec in zip(valid, search_texts, vecs):
        r = _do_search(question, search_text, vec, top_k, threshold)
        r["id"] = qid
        out[i] = r

    return out  # type: ignore[return-value]


def format_as_context(results: list[dict]) -> str:
    if not results:
        return "[知识库检索无结果]"
    parts = []
    for i, r in enumerate(results, 1):
        text = r["text"]
        if len(text) > 2000:
            text = text[:2000] + "...(截断)"
        sim_str = f"{r['similarity']:.2%}" if r["similarity"] is not None else "关键词命中"
        parts.append(f"【知识片段 {i}】📄 {r['file_name']} (相关度: {sim_str})\n{text}")
    return "\n\n---\n\n".join(parts)


# ============================================================
# 常驻 HTTP Server
# ============================================================

_server_start = time.time()
_last_activity = time.time()


def _touch_activity():
    global _last_activity
    _last_activity = time.time()


def _check_auth(auth_header: str | None) -> bool:
    if not QUERY_AUTH_TOKEN:
        return True
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth_header[len("Bearer "):].strip(), QUERY_AUTH_TOKEN)


def _idle_watcher(server: ThreadingHTTPServer):
    while True:
        time.sleep(5)
        if (time.time() - _last_activity) >= IDLE_TIMEOUT:
            try:
                server.shutdown()
            except Exception:
                pass
            _remove_pid(PORT)
            return


class Handler(BaseHTTPRequestHandler):
    server_version = "doc2kb_query_skill/1.0"

    def _send_json(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _touch_activity()
        if self.path == "/health":
            self._send_json(200, {
                "ok": True,
                "uptime": round(time.time() - _server_start, 3),
                "idle_timeout": IDLE_TIMEOUT,
                "db_path": _db_identity(),
                "table": TABLE_NAME,
                "model": EMBEDDING_MODEL,
            })
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    _MAX_BODY_BYTES = 2 * 1024 * 1024

    def do_POST(self):
        _touch_activity()
        if not _check_auth(self.headers.get("Authorization")):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length > self._MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": f"请求体过大（>{self._MAX_BODY_BYTES} 字节）"})
            return
        body   = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"invalid json: {e}"})
            return

        action    = payload.get("action", "search")
        top_k     = int(payload.get("top_k", TOP_K))
        threshold = float(payload.get("threshold", SIMILARITY_THRESHOLD))

        if action == "search":
            self._send_json(200, {"ok": True, "result": search(str(payload.get("question", "")), top_k, threshold)})
        elif action == "batch":
            self._send_json(200, {"ok": True, "result": search_batch(payload.get("items", []), top_k, threshold)})
        elif action == "context":
            r = search(str(payload.get("question", "")), top_k, threshold)
            self._send_json(200, {"ok": True, "result": format_as_context(r.get("results", []))})
        else:
            self._send_json(400, {"ok": False, "error": f"unknown action: {action}"})

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


def _write_pid(port: int):
    try:
        _pid_file_for_port(port).write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def _remove_pid(port: int):
    try:
        p = _pid_file_for_port(port)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def run_server():
    """启动常驻 HTTP server（阻塞）。"""
    _write_pid(PORT)
    if not QUERY_AUTH_TOKEN and HOST not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[WARN] 未设置 DOC2KB_QUERY_AUTH_TOKEN，且监听地址是 {HOST}，"
            f"任何能访问此端口的人都可以查询知识库内容。",
            file=sys.stderr,
        )
    try:
        server  = ThreadingHTTPServer((HOST, PORT), Handler)
        watcher = threading.Thread(target=_idle_watcher, args=(server,), daemon=True)
        watcher.start()
        print(json.dumps({
            "ok": True, "host": HOST, "port": PORT,
            "idle_timeout": IDLE_TIMEOUT, "pid": os.getpid(),
            "db_path": _db_identity(), "model": EMBEDDING_MODEL,
        }, ensure_ascii=False), flush=True)
        server.serve_forever(poll_interval=1)
    finally:
        _remove_pid(PORT)


def _server_health(host: str, port: int, timeout: float = 0.6) -> dict | None:
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _find_or_start_server() -> int:
    my_id = _db_identity()

    for p in range(PORT, PORT + PORT_SCAN_RANGE):
        info = _server_health(HOST, p)
        if info is not None and info.get("db_path") == my_id:
            return p

    free_port = None
    for p in range(PORT, PORT + PORT_SCAN_RANGE):
        if _server_health(HOST, p) is None:
            free_port = p
            break
    if free_port is None:
        raise RuntimeError(
            f"端口 {PORT}-{PORT + PORT_SCAN_RANGE - 1} 都已被其它 server 占用，"
            f"且都不是当前知识库（{my_id}）。请在 .env 里调大 "
            f"DOC2KB_QUERY_PORT_SCAN_RANGE，或换一个 DOC2KB_QUERY_PORT。"
        )

    cmd = [sys.executable, os.path.abspath(__file__), "--server",
           "--host", HOST, "--port", str(free_port)]
    
    # 【改动】不再向 log_file 写日志文件，直接将 stdout 和 stderr 对接到系统控制台
    subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)

    deadline = time.time() + SERVER_START_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.5)
        info = _server_health(HOST, free_port)
        if info is not None and info.get("db_path") == my_id:
            return free_port

    raise TimeoutError(
        f"无法在 {HOST}:{free_port} 启动 server（等待 {SERVER_START_TIMEOUT}s 超时）。\n"
        f"若是首次运行且 embedding 模型还在下载，可以稍等后重试，"
        f"或调大 .env 里的 DOC2KB_QUERY_SERVER_START_TIMEOUT。\n"
        f"提示：请查看上方控制台的报错输出以获取具体失败原因。"
    )


# ============================================================
# CLI 入口
# ============================================================

def main():
    global HOST, PORT
    parser = argparse.ArgumentParser(description="doc2kb 知识库查询")
    parser.add_argument("--question", "-q",  help="问题文本")
    parser.add_argument("--batch",           help="批量问题 JSON 数组")
    parser.add_argument("--format", choices=["json", "context"], default="json")
    parser.add_argument("--server", action="store_true", help="启动常驻检索服务")
    parser.add_argument("--host",   default=HOST)
    parser.add_argument("--port",   type=int, default=PORT)
    args = parser.parse_args()

    HOST = args.host
    PORT = args.port

    if args.server:
        run_server()
        return

    active_port = _find_or_start_server()

    import urllib.request
    if args.batch:
        payload: dict[str, Any] = {
            "action": "batch",
            "items": json.loads(args.batch),
        }
    elif args.question:
        payload = {
            "action":   "context" if args.format == "context" else "search",
            "question": args.question,
        }
    else:
        parser.error("--question 和 --batch 至少需要一个")

    req = urllib.request.Request(
        f"http://{HOST}:{active_port}/",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {QUERY_AUTH_TOKEN}"} if QUERY_AUTH_TOKEN else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if not data.get("ok"):
        raise SystemExit(data)

    result = data["result"]
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()