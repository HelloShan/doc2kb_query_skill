"""
测试 ingest.py::_get_embed_model() 和 query_reference/server.py::_get_embedding_model()
的并发初始化线程安全性。

背景：多个线程第一次同时调用时（模型还没缓存），如果没加锁，每个线程都会
各自触发一次 TextEmbedding(...) 初始化——模型没在本地缓存好时就是好几个
线程同时去下载同一个模型，浪费带宽、加重网络不稳时的下载失败概率。

这里用一个耗时的打桩 TextEmbedding 构造函数模拟"正在下载/初始化"的过程，
验证并发调用时底层构造函数确实只被执行一次。
"""
import sys
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "query_reference"))

import ingest  # noqa: E402
import server  # noqa: E402


class _SlowFakeTextEmbedding:
    """模拟耗时的模型初始化（比如正在下载）。"""
    def __init__(self, model_name=None, max_length=None, **kwargs):
        time.sleep(0.2)
        self.model_name = model_name


def test_ingest_get_embed_model_only_initializes_once_under_concurrency(monkeypatch=None):
    import fastembed
    call_count = {"n": 0}
    real_init = _SlowFakeTextEmbedding.__init__

    class _CountingFakeTextEmbedding(_SlowFakeTextEmbedding):
        def __init__(self, *a, **kw):
            call_count["n"] += 1
            real_init(self, *a, **kw)

    # monkeypatch：直接换掉 fastembed.TextEmbedding
    original = fastembed.TextEmbedding
    fastembed.TextEmbedding = _CountingFakeTextEmbedding
    ingest._embed_model = None  # 重置单例，模拟"第一次调用"
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: ingest._get_embed_model(), range(8)))

        assert call_count["n"] == 1, f"底层 TextEmbedding 应该只被初始化 1 次，实际 {call_count['n']} 次"
        assert all(r is results[0] for r in results), "所有线程应该拿到同一个模型实例"
    finally:
        fastembed.TextEmbedding = original
        ingest._embed_model = None
    print("test_ingest_get_embed_model_only_initializes_once_under_concurrency OK")


def test_server_get_embedding_model_only_initializes_once_under_concurrency():
    import fastembed
    call_count = {"n": 0}
    real_init = _SlowFakeTextEmbedding.__init__

    class _CountingFakeTextEmbedding(_SlowFakeTextEmbedding):
        def __init__(self, *a, **kw):
            call_count["n"] += 1
            real_init(self, *a, **kw)

    original = fastembed.TextEmbedding
    fastembed.TextEmbedding = _CountingFakeTextEmbedding
    server._embedding_model = None
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: server._get_embedding_model(), range(8)))

        assert call_count["n"] == 1, f"底层 TextEmbedding 应该只被初始化 1 次，实际 {call_count['n']} 次"
        assert all(r is results[0] for r in results), "所有线程应该拿到同一个模型实例"
    finally:
        fastembed.TextEmbedding = original
        server._embedding_model = None
    print("test_server_get_embedding_model_only_initializes_once_under_concurrency OK")


def test_ingest_get_embed_model_returns_cached_instance_when_already_set():
    """已经初始化过之后，不应该再触发任何新的构造。"""
    sentinel = object()
    ingest._embed_model = sentinel
    try:
        assert ingest._get_embed_model() is sentinel
    finally:
        ingest._embed_model = None
    print("test_ingest_get_embed_model_returns_cached_instance_when_already_set OK")


def test_ingest_get_embed_model_disables_cpu_mem_arena():
    """
    验证 _get_embed_model() 确实把 enable_cpu_mem_arena=False 传给了
    TextEmbedding 构造函数——这是关掉 onnxruntime CPU 内存 arena
    （只增不减，长时间批量跑容易把内存占用推得越来越高）的开关。
    """
    import fastembed
    captured_kwargs = {}

    class _CapturingFakeTextEmbedding:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)

    original = fastembed.TextEmbedding
    fastembed.TextEmbedding = _CapturingFakeTextEmbedding
    ingest._embed_model = None
    try:
        ingest._get_embed_model()
        assert captured_kwargs.get("enable_cpu_mem_arena") is False, \
            f"应该传 enable_cpu_mem_arena=False，实际收到的参数: {captured_kwargs}"
    finally:
        fastembed.TextEmbedding = original
        ingest._embed_model = None
    print("test_ingest_get_embed_model_disables_cpu_mem_arena OK")


def test_server_get_embedding_model_disables_cpu_mem_arena():
    import fastembed
    captured_kwargs = {}

    class _CapturingFakeTextEmbedding:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)

    original = fastembed.TextEmbedding
    fastembed.TextEmbedding = _CapturingFakeTextEmbedding
    server._embedding_model = None
    try:
        server._get_embedding_model()
        assert captured_kwargs.get("enable_cpu_mem_arena") is False, \
            f"应该传 enable_cpu_mem_arena=False，实际收到的参数: {captured_kwargs}"
    finally:
        fastembed.TextEmbedding = original
        server._embedding_model = None
    print("test_server_get_embedding_model_disables_cpu_mem_arena OK")


if __name__ == "__main__":
    test_ingest_get_embed_model_only_initializes_once_under_concurrency()
    test_server_get_embedding_model_only_initializes_once_under_concurrency()
    test_ingest_get_embed_model_returns_cached_instance_when_already_set()
    test_ingest_get_embed_model_disables_cpu_mem_arena()
    test_server_get_embedding_model_disables_cpu_mem_arena()
    print("\n全部通过 ✅")
