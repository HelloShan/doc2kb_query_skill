#!/usr/bin/env python3
"""
doc2kb — 统一文档流水线 CLI
==============================
将原始文档（docx/pdf/md/txt/pptx）→ Markdown → LanceDB 向量知识库，
使用 SHA256 追踪文件变更，支持增量/全量/重试等多种模式。

用法
----
  # 完整流水线（增量模式：只处理变更和失败的文件）
  python doc_pipeline.py build

  # 完整流水线（全量重建）
  python doc_pipeline.py build --full

  # 仅进行文档转换（docx/pdf → md）
  python doc_pipeline.py build --convert-only

  # 仅进行向量入库（处理已有 .md 文件）
  python doc_pipeline.py build --ingest-only

  # 重试所有失败的文件
  python doc_pipeline.py retry

  # 仅重试转换失败的
  python doc_pipeline.py retry --convert

  # 仅重试入库失败的
  python doc_pipeline.py retry --ingest

  # 查看流水线状态汇总
  python doc_pipeline.py status

  # 列出失败文件明细
  python doc_pipeline.py list-failed

  # 查看知识库统计
  python doc_pipeline.py stats

  # 从知识库中删除文档
  python doc_pipeline.py delete --list-sources      # 列出所有源文件
  python doc_pipeline.py delete --source "file.md"   # 按 source 精确删除
  python doc_pipeline.py delete --prefix "废弃/"     # 按前缀删除
  python doc_pipeline.py delete --all                # 清空整个知识库

跨平台：支持 Windows 11 / Linux / macOS
最低配置：2 线程, 2GB 内存
"""

import argparse
import sys
import time
import os
import signal
from pathlib import Path
from datetime import datetime
from typing import List, Tuple

# 确保 Ctrl+C 能中断 Python 代码（解决 ThreadPoolExecutor 不响应的问题）
signal.signal(signal.SIGINT, signal.SIG_DFL)

# 确保项目根目录在 sys.path 中
_SCRIPT_DIR = Path(__file__).parent.absolute()
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from config import (
    SOURCE_DIR, OUTPUT_MD_DIR, DB_PATH, STATE_FILE, LOG_FILE,
    SUPPORTED_EXTENSIONS, CONVERT_WORKERS, COLOR_OUTPUT,
    validate_config,
)
from state import (
    PipelineState, compute_sha256, collect_files, get_file_mtime_iso,
    ST_OK, ST_SKIP, ST_GARBLED, ST_EMPTY, ST_ERROR, ST_PENDING,
    RETRYABLE_STATUSES,
)
from convert import convert_single_file, scan_compatibility
from ingest import (
    ingest_single_md, rebuild_table, get_db_stats, close_db,
    _open_or_create_db,
)


# ============================================================
# 日志工具
# ============================================================

class Logger:
    """简易彩色日志器"""

    def __init__(self, color: bool = COLOR_OUTPUT, log_path: Path = None):
        self._color = color and sys.stdout.isatty()
        self._log_file = open(log_path, "a", encoding="utf-8") if log_path else None

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._color else text

    def info(self, msg: str):
        line = f"[{self._ts()}] {msg}"
        print(line, file=sys.stderr)
        if self._log_file:
            self._log_file.write(line + "\n")
            self._log_file.flush()

    def ok(self, msg: str):
        line = f"[{self._ts()}] {self._c('32', '✓')} {msg}"
        print(line, file=sys.stderr)
        if self._log_file:
            self._log_file.write(f"[{self._ts()}] OK {msg}\n")
            self._log_file.flush()

    def warn(self, msg: str):
        line = f"[{self._ts()}] {self._c('33', '⚠')} {msg}"
        print(line, file=sys.stderr)
        if self._log_file:
            self._log_file.write(f"[{self._ts()}] WARN {msg}\n")
            self._log_file.flush()

    def err(self, msg: str):
        line = f"[{self._ts()}] {self._c('31', '✗')} {msg}"
        print(line, file=sys.stderr)
        if self._log_file:
            self._log_file.write(f"[{self._ts()}] ERROR {msg}\n")
            self._log_file.flush()

    def close(self):
        if self._log_file:
            self._log_file.close()


# ============================================================
# 进度统计
# ============================================================

class ProgressStats:
    """跟踪流水线各阶段的统计"""

    def __init__(self):
        self.convert = {"ok": 0, "skip": 0, "garbled": 0, "empty": 0, "error": 0, "pending": 0, "total": 0}
        self.ingest = {"ok": 0, "skip": 0, "garbled": 0, "empty": 0, "error": 0, "pending": 0, "total": 0}
        self.total_chunks = 0
        # 按错误消息分组的失败文件，用于最后汇总打印
        self.failed_by_error: dict[str, list[tuple[str, str]]] = {}
        # 转换前的统计（用于增量展示）
        self.convert_new = 0
        self.convert_changed = 0
        self.convert_retry = 0
        self.convert_retry_ingest = 0
        self.convert_skipped = 0
        # 进度计数器
        self.convert_processed = 0
        self.convert_total = 0
        self.ingest_processed = 0
        self.ingest_total = 0

    def add_convert_file_info(self, reason: str):
        """记录文件进入转换队列的原因"""
        if reason == "new":
            self.convert_new += 1
        elif reason == "changed":
            self.convert_changed += 1
        elif reason == "retry":
            self.convert_retry += 1
        elif reason == "retry_ingest":
            self.convert_retry_ingest += 1
        elif reason == "skip":
            self.convert_skipped += 1

    def convert_done(self, result: dict):
        st = result["status"]
        if st in self.convert:
            self.convert[st] += 1
        self.convert["total"] += 1
        # 收集转换失败的按错误分类
        if st in ("error", "garbled", "empty"):
            error = result.get("error") or st
            short_error = error[:120] if len(error) > 120 else error
            if short_error not in self.failed_by_error:
                self.failed_by_error[short_error] = []
            self.failed_by_error[short_error].append(
                (result["rel_path"], st)
            )

    def ingest_done(self, result: dict):
        st = result["status"]
        if st in self.ingest:
            self.ingest[st] += 1
        self.ingest["total"] += 1
        if result.get("chunks"):
            self.total_chunks += result["chunks"]

    def summary(self) -> str:
        conv = self.convert
        ing = self.ingest
        return (
            f"  转换: {conv['ok']} OK, {conv['skip']} 跳过, "
            f"{conv['garbled']} 乱码, {conv['empty']} 空, "
            f"{conv['error']} 错误 / 共{conv['total']}文件\n"
            f"  入库: {ing['ok']} OK, {ing['skip']} 跳过, "
            f"{ing['garbled']} 乱码, {ing['empty']} 空, "
            f"{ing['error']} 错误 / 共{ing['total']}文件, "
            f"总计 {self.total_chunks} 个向量分块"
        )


# ============================================================
# 核心流水线逻辑
# ============================================================

def run_build(args, log: Logger):
    """执行 build 子命令"""

    is_full = args.full
    convert_only = args.convert_only
    ingest_only = args.ingest_only

    if is_full:
        log.warn("全量重建模式：将清空现有知识库并重新处理所有文件")

    log.info(f"源文档目录: {SOURCE_DIR}")
    log.info(f"MD输出目录: {OUTPUT_MD_DIR}")
    log.info(f"知识库路径: {DB_PATH}")
    log.info(f"状态文件:   {STATE_FILE}")
    log.info(f"并行线程:   {CONVERT_WORKERS}")
    log.info("")

    # 创建必要的目录
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD_DIR.mkdir(parents=True, exist_ok=True)

    # 加载状态
    state = PipelineState(STATE_FILE)
    stats = ProgressStats()

    # 扫描源文件
    log.info("🔍 扫描源文件...")
    all_files = collect_files(SOURCE_DIR, SUPPORTED_EXTENSIONS)
    log.info(f"   找到 {len(all_files)} 个源文件")

    if not all_files and not ingest_only:
        log.warn("源目录中没有支持的文档文件")
        log.info(f"支持的格式: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return

    # ── 全量重建：清空状态和知识库 ──
    if is_full:
        log.info("🗑  清空状态和知识库...")
        # 删除旧的状态文件
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        # 重建 LanceDB 表
        rebuild_table()
        state = PipelineState(STATE_FILE)
        log.ok("已清空，准备全量重建")
        log.info("")

    # ── 确定待处理的文件列表 ──
    files_to_convert = []
    files_to_ingest = []

    for fp in all_files:
        rel = fp.relative_to(SOURCE_DIR).as_posix()
        sha256 = compute_sha256(fp)
        stat = fp.stat()
        from datetime import datetime, timezone
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")

        if is_full:
            # 全量模式：所有文件都需要处理
            state.init_file(rel, sha256, stat.st_size, mtime, str(fp.resolve()))
            if not convert_only and not ingest_only:
                files_to_convert.append(fp)
                stats.add_convert_file_info("new")
            elif convert_only:
                files_to_convert.append(fp)
                stats.add_convert_file_info("new")
        else:
            # 增量模式：只处理变更或失败的文件
            entry = state.get_file_state(rel)
            if state.needs_rebuild(rel, sha256):
                state.init_file(rel, sha256, stat.st_size, mtime, str(fp.resolve()))

                # 关键优化：如果转换已完成但入库未完成，直接进 ingest 队列，不重复转换
                if state.is_convert_done(rel):
                    if not convert_only:
                        md_path = OUTPUT_MD_DIR / Path(rel).with_suffix(".md")
                        if md_path.exists():
                            files_to_ingest.append((md_path, rel, sha256))
                        stats.add_convert_file_info("retry_ingest")
                    else:
                        stats.add_convert_file_info("skip")
                elif not ingest_only:
                    files_to_convert.append(fp)
                    # 判断原因：新文件、已变更、还是上次失败
                    if entry is None:
                        stats.add_convert_file_info("new")
                    elif entry.get("sha256") != sha256:
                        stats.add_convert_file_info("changed")
                    else:
                        stats.add_convert_file_info("retry")
            else:
                # 文件未变更且转换已成功，跳过
                state.init_file(rel, sha256, stat.st_size, mtime, str(fp.resolve()))
                stats.add_convert_file_info("skip")

    # 入库文件列表：
    # 如果 ingest_only，从状态中找所有已完成转换但未入库的文件
    if ingest_only:
        if not all_files and OUTPUT_MD_DIR.exists():
            # 源目录无文件 + OUTPUT_MD_DIR 有 .md 文件 = 从其他设备迁移的场景
            log.info("  源目录为空，已从 OUTPUT_MD_DIR 扫描 .md 文件进行入库...")
            md_files = sorted(OUTPUT_MD_DIR.rglob("*.md"))
            for md_path in md_files:
                rel = md_path.relative_to(OUTPUT_MD_DIR).as_posix()
                sha256 = compute_sha256(md_path)
                # 在状态中标记为转换完成，以便 ingestion 回写时状态一致
                state.init_file(rel, sha256,
                                size=md_path.stat().st_size,
                                mtime=get_file_mtime_iso(md_path),
                                abs_source_path=str(md_path.resolve()))
                state.update_conversion(rel, "ok",
                                        md_path=str(md_path.resolve()))
                if not state.is_ingest_done(rel):
                    files_to_ingest.append((md_path, rel, sha256))
            if not md_files:
                log.warn("  OUTPUT_MD_DIR 中没有找到 .md 文件")
            else:
                log.info(f"  待入库（OUTPUT_MD_DIR 扫描）: {len(files_to_ingest)} 个 MD 文件")
        else:
            for fp in all_files:
                rel = fp.relative_to(SOURCE_DIR).as_posix()
                if state.is_convert_done(rel) and not state.is_ingest_done(rel):
                    md_path = OUTPUT_MD_DIR / Path(rel).with_suffix(".md")
                    if md_path.exists():
                        files_to_ingest.append((md_path, rel, state.get_file_sha256(rel)))
            log.info(f"  待入库（仅新建入库）: {len(files_to_ingest)} 个 MD 文件")
    elif not convert_only:
        # 正常流水线：转换完还要入库
        if is_full:
            for fp in all_files:
                rel = fp.relative_to(SOURCE_DIR).as_posix()
                md_path = OUTPUT_MD_DIR / Path(rel).with_suffix(".md")
                if md_path.exists():
                    files_to_ingest.append((md_path, rel, state.get_file_sha256(rel)))
            log.info(f"  待入库（全量）: {len(files_to_ingest)} 个 MD 文件")
        # 增量入库在转换完成后自动添加

    # ── 扫描完成，立即持久化初始状态（防止中途杀进程丢进度）──
    state.save()

    # ── Stage 1: 文档转换 ──
    if files_to_convert and not ingest_only:
        stage_label = "1/2 (仅转换)" if convert_only else "1/2"
        log.info(f"\n📄 阶段 {stage_label}: 文档转换")
        # 打印增量/全量明细
        if not is_full:
            parts = []
            if stats.convert_new:
                parts.append(f"新增 {stats.convert_new}")
            if stats.convert_changed:
                parts.append(f"变更 {stats.convert_changed}")
            if stats.convert_retry:
                parts.append(f"重试转换 {stats.convert_retry}")
            if stats.convert_retry_ingest:
                parts.append(f"直接入库 {stats.convert_retry_ingest}")
            if stats.convert_skipped:
                parts.append(f"跳过 {stats.convert_skipped} (未变更)")
            log.info("  " + ", ".join(parts) + f"，共 {len(files_to_convert)} 个文件")
        else:
            log.info(f"  全量 {len(files_to_convert)} 个文件")
        t0 = time.time()

        from convert import convert_batch
        stats.convert_total = len(files_to_convert)
        stats.convert_processed = 0
        conv_results = convert_batch(
            files_to_convert,
            max_workers=CONVERT_WORKERS,
            progress_callback=lambda r: _on_convert_done(r, state, stats, log),
        )

        elapsed = time.time() - t0
        log.info(f"\n转换完成: {elapsed:.1f}s")
        log.info(stats.summary())

        # 打印转换失败的按错误分组汇总
        if stats.failed_by_error:
            total_failed = sum(len(files) for files in stats.failed_by_error.values())
            log.warn(f"\n⚠  转换失败文件汇总（共 {total_failed} 个，按错误类型分组）：")
            for i, (err_msg, files) in enumerate(stats.failed_by_error.items(), 1):
                log.info(f"  [{i}] {err_msg}")
                for rel_path, st in files[:5]:
                    log.info(f"      [{st}] {rel_path}")
                if len(files) > 5:
                    log.info(f"      ... 还有 {len(files) - 5} 个同类错误文件")
            log.info("")
            log.info("  使用 'python doc_pipeline.py list-failed' 查看全部")
            if convert_only:
                log.info("  使用 'python doc_pipeline.py retry --convert-only' 重试失败文件")

        # 收集成功转换的 MD 文件，准备下一阶段
        if not convert_only:
            for r in conv_results:
                if r["status"] == "ok" and r["md_path"]:
                    md_abs = Path(r["md_path"])  # md_path 已是绝对路径
                    files_to_ingest.append((md_abs, r["rel_path"], r["sha256"]))

    # ── Stage 2: 向量入库 ──
    if files_to_ingest and not convert_only:
        log.info(f"\n🧠 阶段 2/2: 向量入库 ({len(files_to_ingest)} 个文件)...")
        t0 = time.time()

        from ingest import ingest_batch
        stats.ingest_total = len(files_to_ingest)
        stats.ingest_processed = 0
        ing_results = ingest_batch(
            files_to_ingest,
            max_workers=CONVERT_WORKERS,
            progress_callback=lambda r: _on_ingest_done(r, state, stats, log),
        )

        elapsed = time.time() - t0
        log.info(f"\n入库完成: {elapsed:.1f}s")
        log.info(stats.summary())

    # ── 最终保存状态 ──
    state.save()

    # ── 汇总报告 ──
    _print_final_report(state, stats, log)


def _on_convert_done(result: dict, state: PipelineState,
                     stats: ProgressStats, log: Logger):
    """单个文件转换完成的回调"""
    rel = result["rel_path"]
    status = result["status"]
    error = result.get("error")

    state.update_conversion(rel, status,
                            md_path=result.get("md_path"),
                            error=error)
    stats.convert_done(result)
    stats.convert_processed += 1
    state.save()  # 实时写盘，防止杀进程丢进度

    progress = f"[{stats.convert_processed}/{stats.convert_total}]"
    if status == "ok":
        warning = result.get("warning")
        if warning:
            log.warn(f"  ⚠ {progress} {rel}: {warning}")
        else:
            log.ok(f"  {progress} {rel}")
    elif status == "skip":
        log.warn(f"  跳过 {rel}: {error}")
    elif status == "garbled":
        log.err(f"  乱码 {rel}: {error}")
    elif status == "empty":
        log.warn(f"  空文件 {rel}: {error}")
    else:
        log.err(f"  失败 {rel}: {error}")


def _on_ingest_done(result: dict, state: PipelineState,
                    stats: ProgressStats, log: Logger):
    """单个文件入库完成的回调"""
    rel = result["rel_path"]
    status = result["status"]
    error = result.get("error")

    state.update_ingestion(rel, status,
                           chunks=result.get("chunks", 0),
                           error=error)
    stats.ingest_done(result)
    stats.ingest_processed += 1
    state.save()  # 实时写盘，防止杀进程丢进度

    progress = f"[{stats.ingest_processed}/{stats.ingest_total}]"
    if status == "ok":
        log.ok(f"  {progress} {rel} ({result.get('chunks', 0)} chunks)")
    else:
        log.err(f"  入库失败 {rel}: {error}")


def _print_final_report(state: PipelineState, stats: ProgressStats,
                        log: Logger):
    """打印最终汇总报告"""
    summary = state.get_summary()
    log.info("")
    log.info("=" * 60)
    log.info("📊 流水线执行完毕")
    log.info("=" * 60)
    log.info(f"  源文件总数:    {summary['total']}")
    log.info("")
    log.info("  转换状态:")
    for st, count in summary["conversion"].items():
        if count > 0:
            log.info(f"    {st}: {count}")
    log.info("")
    log.info("  入库状态:")
    for st, count in summary["ingestion"].items():
        if count > 0:
            log.info(f"    {st}: {count}")
    log.info("")
    log.info(f"  向量分块总数: {stats.total_chunks}")

    # 检查是否有失败的文件
    failed = state.get_failed_files("conversion")
    if failed:
        # 按错误消息分组
        by_error: dict[str, list[tuple[str, str]]] = {}
        for rel, entry in failed:
            conv = entry.get("conversion", {})
            st = conv.get("status", "?") 
            err = conv.get("error") or st
            short_err = err[:100] if len(err) > 100 else err
            by_error.setdefault(short_err, []).append((rel, st))

        log.warn(f"\n⚠  有 {len(failed)} 个文件转换失败（按错误类型）：")
        for i, (err_msg, files) in enumerate(by_error.items(), 1):
            log.info(f"  [{i}] {err_msg}")
            for rel_path, st in files[:5]:
                log.info(f"      [{st}] {rel_path}")
            if len(files) > 5:
                log.info(f"      ... 还有 {len(files) - 5} 个")
        log.info("")
        log.info("   使用 'python doc_pipeline.py list-failed' 查看全部")
        log.info("   使用 'python doc_pipeline.py retry --convert-only' 重试")

    # 知识库统计
    try:
        db_stats = get_db_stats()
        if "total_chunks" in db_stats:
            log.info(f"\n📚 知识库: {db_stats['total_chunks']} 个向量分块")
    except Exception:
        pass

    log.info("=" * 60)


# ============================================================
# retry 子命令
# ============================================================

def run_retry(args, log: Logger):
    """重试失败的文件"""
    state = PipelineState(STATE_FILE)
    stage = "conversion" if args.convert else ("ingestion" if args.ingest else "all")

    failed = state.get_failed_files(stage)
    if not failed:
        log.ok("没有需要重试的文件")
        return

    log.info(f"找到 {len(failed)} 个需要重试的文件（阶段: {stage}）")

    # 重置状态
    for rel, _ in failed:
        state.reset_file(rel)
    state.save()

    # 构造伪 args 调用 build
    class FakeArgs:
        full = False
        convert_only = getattr(args, 'convert_only', False) or stage == "conversion"
        ingest_only = getattr(args, 'ingest_only', False) or stage == "ingestion"
        retry = False

    run_build(FakeArgs(), log)


# ============================================================
# status 子命令
# ============================================================

def run_status(args, log: Logger):
    """查看流水线状态"""
    if not STATE_FILE.exists():
        log.info("流水线尚未运行过，没有状态记录")
        return

    state = PipelineState(STATE_FILE)
    summary = state.get_summary()

    log.info("📊 流水线状态汇总")
    log.info(f"  状态文件:   {STATE_FILE}")
    log.info(f"  源文件总数: {summary['total']}")
    log.info("")
    log.info("  转换阶段:")
    for st in ["ok", "skip", "garbled", "empty", "error", "pending"]:
        count = summary["conversion"].get(st, 0)
        if count > 0:
            log.info(f"    {st}: {count}")
    log.info("")
    log.info("  入库阶段:")
    for st in ["ok", "skip", "error", "pending"]:
        count = summary["ingestion"].get(st, 0)
        if count > 0:
            log.info(f"    {st}: {count}")

    # 知识库统计
    try:
        db_stats = get_db_stats()
        if "total_chunks" in db_stats:
            log.info(f"\n📚 知识库:")
            log.info(f"   路径: {db_stats['path']}")
            log.info(f"   向量维度: {db_stats['vector_dim']}")
            log.info(f"   分块总数: {db_stats['total_chunks']}")
            log.info(f"   模型: {db_stats['model']}")
    except Exception as e:
        log.info(f"\n📚 知识库: 无法读取 ({e})")

    # 失败文件
    failed = state.get_failed_files("all")
    if failed:
        log.warn(f"\n⚠  存在 {len(failed)} 个失败文件")
        log.info("   使用 'python doc_pipeline.py list-failed' 查看明细")


# ============================================================
# list-failed 子命令
# ============================================================

def run_list_failed(args, log: Logger):
    """列出失败文件（按错误类型分组）"""
    state = PipelineState(STATE_FILE)

    conv_failed = state.get_failed_files("conversion")
    ing_failed = state.get_failed_files("ingestion")

    has_any = False

    # ── 转换失败 ──
    if conv_failed:
        has_any = True
        # 按错误消息分组
        by_error: dict[str, list[tuple[str, str]]] = {}
        for rel, entry in conv_failed:
            conv = entry.get("conversion", {})
            st = conv.get("status", "?")
            err = conv.get("error") or st
            short_err = err[:100] if len(err) > 100 else err
            by_error.setdefault(short_err, []).append((rel, st))

        log.warn(f"❌ 转换失败（共 {len(conv_failed)} 个文件）：\n")
        for i, (err_msg, files) in enumerate(by_error.items(), 1):
            log.info(f"  [{i}] {err_msg}")
            for rel_path, st in sorted(files):
                log.err(f"      [{st}] {rel_path}")
        log.info("")

    # ── 入库失败 ──
    if ing_failed:
        has_any = True
        by_error: dict[str, list[tuple[str, str]]] = {}
        for rel, entry in ing_failed:
            ing = entry.get("ingestion", {})
            st = ing.get("status", "?")
            err = ing.get("error") or st
            short_err = err[:100] if len(err) > 100 else err
            by_error.setdefault(short_err, []).append((rel, st))

        log.warn(f"❌ 入库失败（共 {len(ing_failed)} 个文件）：\n")
        for i, (err_msg, files) in enumerate(by_error.items(), 1):
            log.info(f"  [{i}] {err_msg}")
            for rel_path, st in sorted(files):
                log.err(f"      [{st}] {rel_path}")
        log.info("")

    if not has_any:
        log.ok("没有失败文件")
    else:
        total = len(conv_failed) + len(ing_failed)
        log.info(f"共 {total} 个失败文件")
        log.info("使用 'python doc_pipeline.py retry' 重试所有失败文件")


# ============================================================
# stats 子命令
# ============================================================

def run_check(args, log: Logger):
    """扫描源目录，检查文件兼容性"""
    log.info("🔍 扫描文件兼容性...")
    log.info(f"  源目录: {SOURCE_DIR}")

    if not SOURCE_DIR.exists():
        log.err(f"目录不存在: {SOURCE_DIR}")
        return

    problems = scan_compatibility(SOURCE_DIR)

    if not problems:
        log.ok("所有文档兼容，无问题文件")
        return

    log.warn(f"发现 {len(problems)} 个不兼容文件：")
    log.info("")
    for fp, reason in problems:
        rel = fp.relative_to(SOURCE_DIR).as_posix()
        size = fp.stat().st_size / 1024
        log.info(f"  📄 {rel}  ({size:.0f} KB)")
        log.info(f"     原因: {reason}")
        log.info("")

    log.warn(f"共 {len(problems)} 个文件需手工处理，处理后重新运行流水线。")


def run_stats(args, log: Logger):
    """查看知识库统计"""
    try:
        db_stats = get_db_stats()
        if "error" in db_stats:
            log.err(f"无法读取知识库: {db_stats['error']}")
            return
        log.info("📚 知识库统计")
        log.info(f"  路径:      {db_stats['path']}")
        log.info(f"  表名:      {db_stats['table']}")
        log.info(f"  向量维度:  {db_stats['vector_dim']}")
        log.info(f"  分块总数:  {db_stats['total_chunks']}")
        log.info(f"  嵌入模型:  {db_stats['model']}")
    except Exception as e:
        log.err(f"无法读取知识库: {e}")


# ============================================================
# delete 子命令
# ============================================================

def run_delete(args, log: Logger):
    """从 LanceDB 知识库中删除文档。"""
    table = _open_or_create_db()
    total_before = table.count_rows()

    # ── 列出所有源文件 ──
    if args.list_sources or (not args.source and not args.prefix and not args.all):
        try:
            arrow_tbl = table.to_arrow()
            unique = sorted(set(arrow_tbl.column('source').to_pylist()))
            log.info(f"📚 知识库包含 {len(unique)} 个源文件，共 {total_before} 个向量分块：\n")
            for src in unique:
                log.info(f"  📄 {src}")
            log.info("")
            log.info("用法：")
            log.info("  python doc_pipeline.py delete --source <文件名>    按精确文件名删除")
            log.info("  python doc_pipeline.py delete --prefix <前缀>     按路径前缀删除（如 '废弃/'）")
            log.info("  python doc_pipeline.py delete --all               清空整个知识库")
            log.info("  python doc_pipeline.py delete --list-sources      列出所有源文件")
            log.info("  所有删除操作默认需确认，加 --yes 跳过确认。")
        except Exception as e:
            log.err(f"无法读取知识库: {e}")
        return

    # ── 构造删除条件 ──
    conditions = []
    if args.source:
        conditions.append(f'source = "{args.source}"')
    if args.prefix:
        prefix_escaped = args.prefix.replace('"', '""')
        conditions.append(f'source LIKE "{prefix_escaped}%"')
    if args.all:
        conditions.clear()  # 忽略 source/prefix

    if not conditions and not args.all:
        log.err("请指定要删除的文档：--source / --prefix / --all")
        return

    # ── 预览影响范围 ──
    where_clause = " AND ".join(conditions)
    if args.all:
        log.warn(f"🗑  将清空整个知识库（共 {total_before} 个分块），此操作不可恢复！")
        arrow_tbl = table.to_arrow()
        affected_sources = sorted(set(arrow_tbl.column('source').to_pylist()))
    else:
        # 预览匹配的源文件 — 直接扫全表筛选
        try:
            arrow_tbl = table.to_arrow()
            all_sources = arrow_tbl.column('source').to_pylist()
            # 内存中做筛选（LIKE 模拟）
            if args.source:
                matched = [s for s in all_sources if s == args.source]
            else:
                matched = [s for s in all_sources if s.startswith(args.prefix)]
        except Exception as e:
            log.err(f"查询失败: {e}")
            return
        affected_sources = sorted(set(matched))
        if not affected_sources:
            log.err("没有匹配的源文件")
            return
        log.info(f"🔍 匹配条件，将删除 {len(affected_sources)} 个源文件：\n")
        for src in affected_sources:
            log.info(f"  📄 {src}")
        log.info(f"\n  共 {len(matched)} 个向量分块将被删除")

    # ── 确认 ──
    if not args.yes:
        log.warn("\n⚠  此操作不可恢复！")
        try:
            confirm = input("  确认删除？[y/N]: ")
        except (EOFError, KeyboardInterrupt):
            log.info("")
            log.warn("已取消")
            return
        if confirm.strip().lower() not in ("y", "yes"):
            log.warn("已取消")
            return

    # ── 执行删除 ──
    try:
        if args.all:
            rebuild_table()
            log.ok(f"知识库已清空（之前共 {total_before} 个分块）")
        else:
            from ingest import _lancedb_lock
            with _lancedb_lock:
                table.delete(where_clause)
            deleted = total_before - table.count_rows()
            log.ok(f"已删除 {len(affected_sources)} 个源文件，共 {deleted} 个向量分块")
    except Exception as e:
        log.err(f"删除失败: {e}")
        return

    # ── 可选：同步清理 pipeline_state.json ──
    if not args.all and args.state:
        from state import PipelineState
        state = PipelineState(STATE_FILE)
        removed = 0
        for src in affected_sources:
            if state.get_file_state(src):
                state.remove_file(src)
                removed += 1
        if removed:
            state.save()
            log.ok(f"已同步清理 pipeline_state 中的 {removed} 条记录")


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="doc2kb",
        description="doc2kb — 文档转换→向量知识库统一流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # build
    build_p = subparsers.add_parser("build", help="执行完整流水线（转换+入库）")
    build_p.add_argument("--full", action="store_true",
                         help="全量重建：清空现有知识库，重新处理所有文件")
    build_p.add_argument("--convert-only", action="store_true",
                         help="仅进行文档转换（不执行向量入库）")
    build_p.add_argument("--ingest-only", action="store_true",
                         help="仅进行向量入库（已转换的 .md 文件入库到 LanceDB）")

    # retry
    retry_p = subparsers.add_parser("retry", help="重试所有失败的文件")
    retry_p.add_argument("--convert", action="store_true",
                         help="仅重试转换失败的")
    retry_p.add_argument("--ingest", action="store_true",
                         help="仅重试入库失败的")
    retry_p.add_argument("--convert-only", action="store_true",
                         help="仅重试转换失败的（等同于 --convert）")
    retry_p.add_argument("--ingest-only", action="store_true",
                         help="仅重试入库失败的（等同于 --ingest）")

    # status
    subparsers.add_parser("status", help="查看流水线状态汇总")

    # list-failed
    subparsers.add_parser("list-failed", help="列出所有失败的文件")

    # stats
    subparsers.add_parser("stats", help="查看知识库统计")

    # check
    subparsers.add_parser("check", help="扫描源目录，检查文件兼容性")

    # delete
    delete_p = subparsers.add_parser("delete", help="从知识库中删除文档")
    delete_p.add_argument("--source", type=str, default=None,
                          help="按精确 source 路径删除，如 'doc/xxx.md'")
    delete_p.add_argument("--prefix", type=str, default=None,
                          help="按 source 路径前缀删除，如 '废弃/'")
    delete_p.add_argument("--all", action="store_true",
                          help="清空整个知识库")
    delete_p.add_argument("--list-sources", action="store_true",
                          help="列出知识库中所有源文件")
    delete_p.add_argument("--yes", "-y", action="store_true",
                          help="跳过确认提示")
    delete_p.add_argument("--state", action="store_true",
                          help="同步清理 pipeline_state.json 中的记录")

    args = parser.parse_args()

    # 初始化日志
    log = Logger(color=COLOR_OUTPUT, log_path=LOG_FILE)

    try:
        if args.command == "build":
            validate_config()
            run_build(args, log)
        elif args.command == "retry":
            run_retry(args, log)
        elif args.command == "status":
            run_status(args, log)
        elif args.command == "list-failed":
            run_list_failed(args, log)
        elif args.command == "check":
            run_check(args, log)
        elif args.command == "stats":
            run_stats(args, log)
        elif args.command == "delete":
            run_delete(args, log)
        else:
            parser.print_help()
    except KeyboardInterrupt:
        log.warn("\n用户中断")
        sys.exit(1)
    except Exception as e:
        import traceback
        log.err(f"未预期的错误: {e}")
        log.info(traceback.format_exc())
        sys.exit(1)
    finally:
        close_db()
        log.close()


if __name__ == "__main__":
    main()
