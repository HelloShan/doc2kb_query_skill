---
name: doc2kb_query
description: >
  查询 make_lancedb 构建的本地技术知识库，回答与已入库文档相关的技术问题。
  适用于：查找公司/项目技术规范、操作手册、配置参数说明、SQL 建表字段含义等。
  不适用于：通用知识问答、网络搜索、代码调试等不在知识库覆盖范围内的任务。
version: "1.0"
skills:
  - name: query_knowledge_base
    description: 对本地 LanceDB 知识库执行 hybrid 检索（向量语义 + BM25 关键词 + RRF 融合）
    parameters:
      question:
        type: string
        description: 用户的问题
        required: true
    returns:
      type: json
      description: 匹配的知识库内容列表，含相似度和置信度
---

# doc2kb 知识库查询 Skill

## 快速开始

### 1. 配置

```bash
cd doc2kb_query/scripts
cp .env.example .env
# 编辑 .env，把 DOC2KB_QUERY_DB_PATH 改成知识库的绝对路径，避免执行脚本当前路径变动时出错
# 以及把 DOC2KB_QUERY_EMBEDDING_MODEL 改成构建时用的模型
```

### 2. 安装依赖

```bash
pip install fastembed lancedb tantivy pyarrow python-dotenv
# 如果用了术语表功能，还需要:
pip install PyYAML
```

### 3. 查询

```bash
# 单题查询（首次会自动在后台拉起常驻 server，模型只加载一次）
python scripts/query.py --question "你的问题"

# 对话友好格式
python scripts/query.py --question "你的问题" --format context

# 批量查询
python scripts/query.py --batch '[{"id":"1","question":"问题A"},{"id":"2","question":"问题B"}]'

# HTTP 接口（server 自动常驻，无需手动管理）
curl http://127.0.0.1:8788/health
curl -X POST http://127.0.0.1:8788/ \
     -H "Authorization: Bearer <口令>" \
     -d '{"action":"search","question":"你的问题","top_k":5}'
```

## 常驻 Server 机制

**模型只加载一次。** 第一次调用 `query.py` 时，脚本检测到没有常驻进程，
会自动在后台拉起一个（冷启动约 5-10 秒，取决于模型大小；首次运行如果
本地还没有模型缓存，需要额外的下载时间），之后所有查询都通过 HTTP
发给这个进程处理，响应毫秒级。空闲超过 `IDLE_TIMEOUT`（默认 1 小时）
后自动退出，下次调用再重新拉起。

```
第一次调用 ──→ 检测到没有 server ──→ 后台拉起 server（加载模型）──→ 转发查询 ──→ 返回结果
后续调用   ──→ 检测到 server 已在跑 ──────────────────────────────→ 转发查询 ──→ 返回结果
```

**同一台机器上部署多个知识库怎么办？** 这个 skill 经常被分发给不同的人
/ 用在不同的知识库上，大家的 `.env` 大多是照抄 `.env.example`，默认端口
都是 `8788`。脚本会在拉起/复用 server 前，先用 `/health` 校验对方服务的
知识库路径是否和自己配置的一致；如果端口已经被**另一个知识库**的 server
占用，会自动在 `[PORT, PORT+DOC2KB_QUERY_PORT_SCAN_RANGE)` 范围内找一个
空闲端口拉起自己的 server，不需要每次都手动改端口，也不会出现"误查到
别人知识库内容"的情况。实际使用的端口可能和 `.env` 里配置的不一样——
可以通过 `.server_<端口>.pid` 文件反查，或者直接看 `/health` 返回的
`db_path` 确认命中的是不是自己的知识库。

重启 server（修改配置后）：
```bash
# 先确认自己知识库实际用的是哪个端口（文件名里带端口号）
ls scripts/.server_*.pid
# Linux
kill $(cat scripts/.server_<端口>.pid)
# Windows
taskkill /PID <scripts/.server_<端口>.pid 里的值> /F
# 之后直接再发一次查询，会自动重新拉起
```

如果自动拉起失败（比如缺依赖、端口范围内确实没有空位），报错信息里会
附上 `scripts/.server_<端口>.startup.log` 的末尾内容，通常能直接看出原因。

## 返回格式

```json
{
  "found": true,
  "query": "原始问题",
  "expanded_query": "术语展开后的问题（若有）",
  "hits": 3,
  "best_similarity": 0.87,
  "confidence": "high",
  "results": [
    {
      "text": "匹配到的知识库原文",
      "file_name": "文档.md",
      "source": "relative/path.md",
      "similarity": 0.87,
      "matched_by": "hybrid",
      "doc_type": "doc"
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `confidence` | `high`（≥0.75）/ `medium`（0.55-0.75）/ `low`（<0.55）|
| `similarity` | 真实余弦相似度，`null` 表示纯关键词命中 |
| `matched_by` | `hybrid`=向量+BM25 双路，`keyword`=仅 BM25 精确命中（型号/缩写等）|
| `doc_type` | `doc`/`sql`/`yaml`/`json`/`ini` |

## Claude 回答规范

**有结果时：**
- 引用原文，标注来源：`📄 [文件名]`
- `confidence: high` → 直接回答
- `confidence: medium` → 给出内容，建议交叉核实
- `confidence: low` → 声明"仅供参考，置信度较低"
- `matched_by: keyword` 结果（型号/缩写精确命中）视为强证据

**无结果时（`found: false`）：**
- 明确告知"知识库中未找到相关内容"
- 不要脑补、不要凭通用知识填充

## 配置项（scripts/.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DOC2KB_QUERY_DB_PATH` | `../doc2kb.lancedb` | 知识库路径，**建议绝对路径** |
| `DOC2KB_QUERY_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | **必须和构建时一致** |
| `DOC2KB_QUERY_EMBEDDING_MAX_TOKENS` | `512` | 随模型同步修改 |
| `DOC2KB_QUERY_TABLE_NAME` | `docs` | LanceDB 表名 |
| `DOC2KB_QUERY_TOP_K` | `5` | 返回结果数 |
| `DOC2KB_QUERY_SIMILARITY_THRESHOLD` | `0.5` | 余弦相似度过滤阈值 |
| `DOC2KB_QUERY_HOST` | `127.0.0.1` | server 监听地址 |
| `DOC2KB_QUERY_PORT` | `8788` | server 端口（若被其它知识库占用，会在扫描范围内自动换一个，见下） |
| `DOC2KB_QUERY_PORT_SCAN_RANGE` | `10` | 端口自动避让的扫描范围，即 `[PORT, PORT+此值)` |
| `DOC2KB_QUERY_SERVER_START_TIMEOUT` | `60` | 等待常驻 server 就绪的超时（秒），首次运行需下载模型时可调大 |
| `DOC2KB_QUERY_IDLE_TIMEOUT` | `3600` | 空闲自动退出（秒）|
| `DOC2KB_QUERY_AUTH_TOKEN` | （空）| 访问口令，空=不鉴权 |
| `DOC2KB_QUERY_GLOSSARY_PATH` | `../glossary.yaml` | 术语表，不存在时跳过 |

> `DOC2KB_QUERY_DB_PATH` / `DOC2KB_QUERY_GLOSSARY_PATH` 配成相对路径时，
> 会按"相对这个脚本所在目录"解析，不依赖你从哪个目录运行命令，可以放心
> 让 Claude/自动化脚本从任意工作目录调用。

## 常见问题

| 错误 | 原因 | 解决 |
|------|------|------|
| `知识库路径不存在` | DB_PATH 路径不对 | 用绝对路径；确认已运行 build |
| `维度不匹配` | 查询和构建模型不一致 | 检查 `EMBEDDING_MODEL` 两边是否一致 |
| `Model XXX is not supported` | fastembed 不支持此模型 | 改用 `bge-small-zh-v1.5` 或 `jina-embeddings-v2-base-zh` |
| 查询无结果但库有内容 | 阈值过高或问法偏差大 | 降低 `SIMILARITY_THRESHOLD`；或配置术语表 |
