"""doc2kb — 流水线状态管理模块
=============================
核心功能：
  1. 对每个源文件记录 SHA256，用于增量检测
  2. 记录转换/入库两阶段的状态、时间戳、错误信息
  3. 支持查询：已处理、已变更、失败（按阶段分类）
  4. 线程安全写入（文件锁）

状态文件格式：pipeline_state.json
{
  "version": 2,
  "pipeline_config_snapshot": { ... },
  "files": {
    "relative/path.docx": {
      "sha256": "abc...",
      "size": 12345,
      "mtime": "2026-06-22T10:00:00",
      "conversion": {
        "status": "ok | garbled | empty | error | skip",
        "md_path": "output_md/relative/path.md",
        "timestamp": "2026-06-22T10:00:05",
        "error": null
      },
      "ingestion": {
        "status": "ok | error | skip",
        "chunks": 42,
        "timestamp": "2026-06-22T10:00:10",
        "error": null
      }
    }
  }
}
"""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor

from config import STATE_FILE

# 状态常量
ST_OK = "ok"
ST_SKIP = "skip"
ST_GARBLED = "garbled"
ST_EMPTY = "empty"
ST_ERROR = "error"
ST_PENDING = "pending"

ALL_STATUSES = {ST_OK, ST_SKIP, ST_GARBLED, ST_EMPTY, ST_ERROR, ST_PENDING}

# 需要重试的状态
RETRYABLE_STATUSES = {ST_GARBLED, ST_EMPTY, ST_ERROR, ST_PENDING}


# ============================================================
# SHA256 计算
# ============================================================

def compute_sha256(file_path: Path) -> str:
    """计算文件的 SHA256 哈希值，分块读取以节省内存。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_file_mtime_iso(file_path: Path) -> str:
    """获取文件最后修改时间的 ISO 格式字符串。"""
    mtime = file_path.stat().st_mtime
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return dt.isoformat(timespec="seconds")


# ============================================================
# 状态条目辅助函数
# ============================================================

def make_conversion_record(status: str, md_path: Optional[str] = None,
                           error: Optional[str] = None) -> dict:
    return {
        "status": status,
        "md_path": md_path,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": error,
    }


def make_ingestion_record(status: str, chunks: int = 0,
                          error: Optional[str] = None) -> dict:
    return {
        "status": status,
        "chunks": chunks,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": error,
    }


# ============================================================
# PipelineState 主类
# ============================================================

class PipelineState:
    """流水线状态管理器"""

    def __init__(self, state_path: Path = STATE_FILE):
        self._state_path = Path(state_path)
        self._dirty = False
        self._data: dict = self._load()

    # ---- 加载/保存 ----

    def _load(self) -> dict:
        if self._state_path.exists():
            try:
                raw = self._state_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if "files" not in data:
                    data["files"] = {}
                return data
            except (json.JSONDecodeError, OSError):
                return {"version": 2, "files": {}}
        return {"version": 2, "files": {}}

    def save(self):
        """持久化状态到磁盘文件。"""
        if not self._dirty:
            return
        # 确保目录存在
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._state_path)
        self._dirty = False

    def mark_dirty(self):
        """标记状态已更改，下次 save() 时写盘。"""
        self._dirty = True

    # ---- 查询 ----

    def get_file_state(self, rel_path: str) -> Optional[dict]:
        """获取单个文件的状态记录，不存在返回 None。"""
        return self._data["files"].get(rel_path)

    def get_file_sha256(self, rel_path: str) -> Optional[str]:
        """获取文件上次记录的 SHA256。"""
        entry = self._data["files"].get(rel_path)
        return entry.get("sha256") if entry else None

    def has_changed(self, rel_path: str, sha256: str) -> bool:
        """
        判断文件是否已变更（SHA256 不同或从未处理）。
        True = 需要处理, False = 无需处理。
        """
        entry = self._data["files"].get(rel_path)
        if entry is None:
            return True  # 新文件
        return entry.get("sha256") != sha256

    def needs_rebuild(self, rel_path: str, sha256: str) -> bool:
        """
        判断文件是否需要重新处理（变更 或 转换/入库失败 或 从未处理）。
        """
        entry = self._data["files"].get(rel_path)
        if entry is None:
            return True  # 新文件
        if entry.get("sha256") != sha256:
            return True  # 文件已变更
        # 检查转换状态
        conv = entry.get("conversion", {})
        if conv.get("status") in RETRYABLE_STATUSES:
            return True
        # 检查入库状态（只在需要入库时）
        ing = entry.get("ingestion", {})
        if ing.get("status") in RETRYABLE_STATUSES:
            return True
        return False

    def is_convert_done(self, rel_path: str) -> bool:
        """文件的转换阶段是否已完成（OK/SKIP = 完成）。"""
        entry = self._data["files"].get(rel_path)
        if entry is None:
            return False
        conv = entry.get("conversion", {})
        return conv.get("status") in (ST_OK, ST_SKIP)

    def is_ingest_done(self, rel_path: str) -> bool:
        """文件的入库阶段是否已完成。"""
        entry = self._data["files"].get(rel_path)
        if entry is None:
            return False
        ing = entry.get("ingestion", {})
        return ing.get("status") in (ST_OK, ST_SKIP)

    # ---- 更新 ----

    def init_file(self, rel_path: str, sha256: str, size: int, mtime: str,
                  abs_source_path: str = ""):
        """初始化文件记录（如果不存在或 SHA256 已变）。"""
        entry = self._data["files"].get(rel_path)
        if entry and entry.get("sha256") == sha256:
            return  # 完全一致，无需更新
        self._data["files"][rel_path] = {
            "sha256": sha256,
            "size": size,
            "mtime": mtime,
            "abs_source_path": abs_source_path,
            "conversion": make_conversion_record(ST_PENDING),
            "ingestion": make_ingestion_record(ST_PENDING),
        }
        self._dirty = True

    def update_conversion(self, rel_path: str, status: str,
                          md_path: Optional[str] = None,
                          error: Optional[str] = None):
        """更新文件的转换状态。"""
        if status not in ALL_STATUSES:
            raise ValueError(f"无效状态: {status}")
        entry = self._data["files"].setdefault(rel_path, {})
        entry["conversion"] = make_conversion_record(status, md_path, error)
        self._dirty = True

    def update_ingestion(self, rel_path: str, status: str,
                         chunks: int = 0, error: Optional[str] = None):
        """更新文件的入库状态。"""
        if status not in ALL_STATUSES:
            raise ValueError(f"无效状态: {status}")
        if status == ST_OK and chunks <= 0:
            raise ValueError("入库成功时 chunks 必须 > 0")
        entry = self._data["files"].setdefault(rel_path, {})
        entry["ingestion"] = make_ingestion_record(status, chunks, error)
        self._dirty = True

    def remove_file(self, rel_path: str):
        """从状态中删除文件记录。"""
        if rel_path in self._data["files"]:
            del self._data["files"][rel_path]
            self._dirty = True

    def reset_file(self, rel_path: str):
        """重置文件状态为待处理。"""
        entry = self._data["files"].get(rel_path)
        if entry:
            entry["conversion"] = make_conversion_record(ST_PENDING)
            entry["ingestion"] = make_ingestion_record(ST_PENDING)
            self._dirty = True

    # ---- 批量查询 ----

    def get_failed_files(self, stage: str = "all") -> List[Tuple[str, dict]]:
        """
        获取失败文件列表。

        Parameters
        ----------
        stage : str
            "all" - 两阶段都查
            "conversion" - 仅转换失败
            "ingestion" - 仅入库失败

        Returns
        -------
        List[(rel_path, entry)]
        """
        results = []
        for rel_path, entry in self._data["files"].items():
            conv = entry.get("conversion", {})
            ing = entry.get("ingestion", {})

            if stage in ("all", "conversion"):
                if conv.get("status") in RETRYABLE_STATUSES:
                    results.append((rel_path, entry))
            if stage in ("all", "ingestion"):
                if ing.get("status") in RETRYABLE_STATUSES:
                    results.append((rel_path, entry))

        return results

    def get_summary(self) -> dict:
        """
        获取流水线状态汇总。

        Returns
        -------
        {
            "total": int,
            "conversion": {"ok": int, "skip": int, "garbled": int, "empty": int, "error": int, "pending": int},
            "ingestion": {"ok": int, "skip": int, "error": int, "pending": int},
        }
        """
        total = len(self._data["files"])

        def count_stage(key, valid_statuses):
            counts = {s: 0 for s in valid_statuses}
            for entry in self._data["files"].values():
                s = entry.get(key, {}).get("status", ST_PENDING)
                if s in counts:
                    counts[s] += 1
            return counts

        conv_counts = count_stage("conversion", ALL_STATUSES)
        ing_counts = count_stage("ingestion", ALL_STATUSES)

        return {
            "total": total,
            "conversion": conv_counts,
            "ingestion": ing_counts,
        }


def collect_files(source_dir: Path, extensions: set) -> List[Path]:
    """
    递归扫描目录，收集所有支持的源文件。
    返回按相对路径排序的文件列表（保证跨平台一致性）。
    """
    files = []
    for ext in extensions:
        # 使用 rglob 递归搜索（跨平台）
        for fp in source_dir.rglob(f"*{ext}"):
            if fp.is_file():
                files.append(fp)

    # 按相对路径排序，保证重复运行时顺序一致
    files.sort(key=lambda p: p.relative_to(source_dir).as_posix())
    return files


def compute_sha256_batch(file_paths: List[Path],
                         max_workers: int = 2) -> Dict[Path, str]:
    """批量计算 SHA256，使用多线程加速。"""
    results = {}

    def _compute(fp: Path) -> Tuple[Path, str]:
        return (fp, compute_sha256(fp))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for fp, sha in pool.map(_compute, file_paths):
            results[fp] = sha

    return results
