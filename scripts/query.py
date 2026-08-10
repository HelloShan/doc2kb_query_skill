#!/usr/bin/env python3
"""
doc2kb 知识库查询 —— 自包含 Skill 脚本
========================================
整合了 query_reference/server.py 的全部逻辑：
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

_SKILL_DIR = Path(__file__).resolve().parent

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


# 知识库身份参数（必须和构建端保持一致）
DB_PATH      = Path(os.environ.get("DOC2KB_QUERY_DB_PATH",        "../doc2kb.lancedb"))
TABLE_NAME   = os.environ.get("DOC2KB_QUERY_TABLE_NAME",           "docs")
EMBEDDING_MODEL    = os.environ.get("DOC2KB_QUERY_EMBEDDING_MODEL",     "BAAI/bge-small-zh-v1.5")
EMBEDDING_MAX_TOKENS = _env_int("DOC2KB_QUERY_EMBEDDING_MAX_TOKENS", 512)
GLOSSARY_PATH = Path(os.environ.get("DOC2KB_QUERY_GLOSSARY_PATH",  "../glossary.yaml"))

# 检索参数
TOP_K               = _env_int("DOC2KB_QUERY_TOP_K",              5)
SIMILARITY_THRESHOLD = _env_float("DOC2KB_QUERY_SIMILARITY_THRESHOLD", 0.5)

# 服务监听
HOST         = os.environ.get("DOC2KB_QUERY_HOST",                 "127.0.0.1")
PORT         = _env_int("DOC2KB_QUERY_PORT",                       8788)
IDLE_TIMEOUT = _env_int("DOC2KB_QUERY_IDLE_TIMEOUT",               3600)
PID_FILE     = os.environ.get("DOC2KB_QUERY_PID_FILE",
                               str(_SKILL_DIR / ".server.pid"))

# 访问口令（可选）
QUERY_AUTH_TOKEN = os.environ.get("DOC2KB_QUERY_AUTH_TOKEN", "").strip()


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
# Embedding 模型（单例 + 双重检查锁定）
# ============================================================

_embedding_model      = None
_embedding_model_lock = threading.Lock()


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        with _embedding_model_lock:
            if _embedding_model is None:
                from fastembed import TextEmbedding
                _embedding_model = TextEmbedding(
                    model_name=EMBEDDING_MODEL,
                    max_length=EMBEDDING_MAX_TOKENS,
                    enable_cpu_mem_arena=False,   # 避免长驻进程内存只增不减
                )
    return _embedding_model


def _get_query_vector(model, question: str) -> list:
    # query_embed() 会自动加 BGE 检索指令前缀，比通用 embed() 准确
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
                f"请先在 doc2kb 项目里运行: python doc_pipeline.py build"
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


def search(
    question: str,
    top_k: int = TOP_K,
    threshold: float = SIMILARITY_THRESHOLD,
) -> dict:
    _touch_activity()
    try:
        _, table = _get_db_and_table()
        model    = _get_embedding_model()
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

    fetch_limit = max(top_k * 3, 10)
    try:
        hybrid_raw = _run_hybrid_search(table, query_vec, search_text, fetch_limit)
    except Exception as e:
        return {"found": False, "query": question, "results": [], "hits": 0, "error": f"检索失败: {e}"}

    # 纯向量检索只用于获取真实余弦相似度（1 - cosine_distance），不参与排序
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
            matched_by = "keyword"   # BM25 精确命中，不受余弦阈值限制
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


def search_batch(items: list[dict], top_k: int = TOP_K, threshold: float = SIMILARITY_THRESHOLD) -> list[dict]:
    out = []
    for item in items:
        qid      = str(item.get("id", ""))
        question = str(item.get("question", "")).strip()
        if not question:
            out.append({"id": qid, "error": "empty question", "found": False, "results": [], "hits": 0})
            continue
        r = search(question, top_k=top_k, threshold=threshold)
        r["id"] = qid
        out.append(r)
    return out


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
            _remove_pid()
            return


class Handler(BaseHTTPRequestHandler):
    server_version = "doc2kb-skill/1.0"

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
                "db_path": str(DB_PATH),
                "model": EMBEDDING_MODEL,
            })
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        _touch_activity()
        if not _check_auth(self.headers.get("Authorization")):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", "0"))
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


def _write_pid():
    try:
        Path(PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def _remove_pid():
    try:
        p = Path(PID_FILE)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def run_server():
    """启动常驻 HTTP server（阻塞）。"""
    _write_pid()
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
        # 启动成功后打印一行 JSON，供调用方/父进程确认 server 已就绪
        print(json.dumps({
            "ok": True, "host": HOST, "port": PORT,
            "idle_timeout": IDLE_TIMEOUT, "pid": os.getpid(),
            "db_path": str(DB_PATH), "model": EMBEDDING_MODEL,
        }, ensure_ascii=False), flush=True)
        server.serve_forever(poll_interval=1)
    finally:
        _remove_pid()


def _server_is_running() -> bool:
    import socket
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except Exception:
        return False


def _ensure_server_running():
    """检测常驻 server 是否在跑，没有则在后台自动拉起，等待就绪。"""
    if _server_is_running():
        return
    cmd = [sys.executable, os.path.abspath(__file__), "--server",
           "--host", HOST, "--port", str(PORT)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):     # 最多等 20 秒
        time.sleep(0.5)
        if _server_is_running():
            return
    raise TimeoutError(f"无法在 {HOST}:{PORT} 启动 server，请检查配置或手动运行 --server")


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

    # 非 server 模式：确保后台 server 在跑，然后通过 HTTP 转发
    _ensure_server_running()

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
        f"http://{HOST}:{PORT}/",
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
