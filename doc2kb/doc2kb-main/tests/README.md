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

# 回归测试

这里的测试是本轮修复（查询崩溃、相似度计算、超时竞争、docm 判重、状态校验）
配套写的最小回归集，不依赖网络下载任何模型（真实 LanceDB 表 + 打桩的 embedding
模型），可以在离线环境直接跑。

> 注：原来还有 `test_server_search.py` / `test_embed_model_thread_safety.py` /
> `test_query_config_isolation.py` 三个文件，测的是 `query_reference/` 这个
> 已经不再使用的旧版查询实现，随着 `query_reference/` 目录一起删掉了。
> `test_embed_model_thread_safety.py` 里原本还顺带测了 `ingest.py::_get_embed_model()`
> 的并发安全性（这部分和 `query_reference` 无关），删除后这部分暂时没有
> 测试覆盖了，如果后续 `ingest.py` 这块逻辑有改动，需要注意补一下。

## 依赖

```bash
pip install lancedb tantivy pyarrow numpy langchain-text-splitters PyYAML python-dotenv --break-system-packages
```

## 运行

```bash
python3 tests/run_all.py
```

或单独跑某一个：

```bash
python3 tests/test_state.py
python3 tests/test_convert_docm.py
python3 tests/test_ingest_abandonment.py
```

## 覆盖内容

- `test_state.py` — `assert` → `raise ValueError` 的修复，包括在 `python -O` 下验证仍然生效；
  `reset_file()` 只重置真正失败/未处理的阶段，不会把已经成功的阶段连带清空
  （修复"retry 把刚转换成功但还没入库的文件重新打回 pending，进度永远攒不起来"这个 bug）
- `test_convert_docm.py` — `_is_docm_file` / `detect_docm` 合并后行为一致
- `test_ingest_abandonment.py` — 超时后台线程的"放弃标记"机制
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
- `test_env_config.py` — `.env` 配置加载：根目录 `config.py` 正确从
  `.env` 读取配置、类型转换正确、没有 `.env` 时正确回退到默认值
