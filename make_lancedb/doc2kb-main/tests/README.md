# 回归测试

这里的测试是本轮修复（查询崩溃、相似度计算、超时竞争、docm 判重、状态校验）
配套写的最小回归集，不依赖网络下载任何模型（真实 LanceDB 表 + 打桩的 embedding
模型），可以在离线环境直接跑。

## 依赖

```bash
pip install lancedb tantivy pyarrow numpy langchain-text-splitters PyYAML python-dotenv --break-system-packages
```

（`fastembed` 不是运行测试必须的：server.py/ingest.py 里对 fastembed 的调用在测试里
全部被打桩替换掉了，避免依赖 HuggingFace 模型下载。）

## 运行

```bash
python3 tests/run_all.py
```

或单独跑某一个：

```bash
python3 tests/test_state.py
python3 tests/test_convert_docm.py
python3 tests/test_ingest_abandonment.py
python3 tests/test_server_search.py
```

## 覆盖内容

- `test_state.py` — `assert` → `raise ValueError` 的修复，包括在 `python -O` 下验证仍然生效
- `test_convert_docm.py` — `_is_docm_file` / `detect_docm` 合并后行为一致
- `test_ingest_abandonment.py` — 超时后台线程的"放弃标记"机制
- `test_server_search.py` — `query_reference/server.py::search()` 端到端：
  - 修复前 `.add_fts_query()` / `reranker="rrf"` 会在任何 lancedb 版本上直接崩溃
  - 相似度改为真实余弦相似度 `1 - cosine_distance`，不再恒等于 1.0
  - 最终结果顺序保持 hybrid+RRF 融合排序，不再被重新按向量距离排序覆盖
  - 纯关键词命中（不在向量候选池里）正确标记为 `matched_by="keyword"` 且不被相似度阈值误杀
  - 旧知识库缺 FTS 索引时能自动补建索引后重试，而不是直接报错
  - `search()` 确实把术语表展开后的文本用于 embedding + FTS 检索
- `test_chunking.py` — SQL/YAML/JSON/INI 语义切分：
  - SQL 按语句切分，正确跳过字符串/注释里的 `;`
  - YAML 按顶层 key 切分，嵌套内容和注释不被打断，文件头部前言并入第一个 key
  - JSON 按顶层 key 切分，解析失败时兜底不炸整个 build
  - INI/conf/TOML 按 `[section]` 切分
  - 小片段合并、超限片段硬切
  - `chunk_markdown_file()` 端到端识别 source_ext 标记并打上正确的 `doc_type`
- `test_ingest_doctype.py` — LanceDB 写入：新表带 `doc_type` 列且值正确；
  旧 schema（无 `doc_type`）的表升级后仍能正常写入，不报错、不改变旧表结构
- `test_glossary.py` — 术语表加载与查询展开：无文件时 no-op、正常展开、
  简写格式、ASCII 术语按单词边界匹配（不误伤子串）、格式错误时静默降级、
  文件改动后按 mtime 自动重新加载
- `test_embed_model_thread_safety.py` — embedding 模型初始化的并发安全性：
  `ingest.py::_get_embed_model()` 和 `query_reference/server.py::_get_embedding_model()`
  在多线程并发首次调用时，底层 `TextEmbedding(...)` 都只应该被初始化一次
  （用 monkeypatch 掉 `fastembed.TextEmbedding` 验证并发调用次数，修复前
  用同样的测试实测确认是 8 个线程各自初始化一次），以及两处都正确关闭了
  onnxruntime 的 CPU 内存 arena（避免批量入库/长驻查询内存只增不减）
- `test_query_config_isolation.py` — 查询端的两个安全机制：
  - 向量维度运行时自检：查询端和构建端是完全独立的两份配置，模型配置
    漂移（维度对不上）时，应该在第一次连接知识库时就用清楚的报错拦下来，
    而不是安静放过去或者在深层 pyarrow 调用里炸出看不懂的错误；且自检
    只应该跑一次，不是每次查询都重新探测
  - 访问口令鉴权：设置 `DOC2KB_QUERY_AUTH_TOKEN` 后拒绝缺少/错误的
    `Authorization` 请求头，未设置时保持原有的直接放行行为
- `test_env_config.py` — `.env` 配置加载：根目录 `config.py` 正确从
  `.env` 读取配置、类型转换正确、没有 `.env` 时正确回退到默认值；
  `query_reference/query_config.py` 读的是自己目录下独立的 `.env`，
  和构建端的配置互不覆盖（用不同的变量名和值分别验证两边读到的确实是
  各自文件里的值，不是对方的）
