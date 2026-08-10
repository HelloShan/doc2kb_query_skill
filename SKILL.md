---
name: doc2kb_query
description: >
  查询 doc2kb 构建的本地技术知识库，回答与已入库文档相关的技术问题。
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
# 编辑 .env，至少把 DOC2KB_QUERY_DB_PATH 改成知识库的绝对路径
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
会自动在后台拉起一个（冷启动约 5-10 秒，取决于模型大小），之后所有查询
都通过 HTTP 发给这个进程处理，响应毫秒级。空闲超过 `IDLE_TIMEOUT`（默认 1 小时）
后自动退出，下次调用再重新拉起。

```
第一次调用 ──→ 检测到没有 server ──→ 后台拉起 server（加载模型）──→ 转发查询 ──→ 返回结果
后续调用   ──→ 检测到 server 已在跑 ──────────────────────────────→ 转发查询 ──→ 返回结果
```

重启 server（修改配置后）：
```bash
# Linux
kill $(cat scripts/.server.pid)
# Windows
taskkill /PID <scripts/.server.pid 里的值> /F
# 之后直接再发一次查询，会自动重新拉起
```

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
| `DOC2KB_QUERY_PORT` | `8788` | server 端口 |
| `DOC2KB_QUERY_IDLE_TIMEOUT` | `3600` | 空闲自动退出（秒）|
| `DOC2KB_QUERY_AUTH_TOKEN` | （空）| 访问口令，空=不鉴权 |
| `DOC2KB_QUERY_GLOSSARY_PATH` | `../glossary.yaml` | 术语表，不存在时跳过 |

## 常见问题

| 错误 | 原因 | 解决 |
|------|------|------|
| `知识库路径不存在` | DB_PATH 路径不对 | 用绝对路径；确认已运行 build |
| `维度不匹配` | 查询和构建模型不一致 | 检查 `EMBEDDING_MODEL` 两边是否一致 |
| `Model XXX is not supported` | fastembed 不支持此模型 | 改用 `bge-small-zh-v1.5` 或 `jina-embeddings-v2-base-zh` |
| 查询无结果但库有内容 | 阈值过高或问法偏差大 | 降低 `SIMILARITY_THRESHOLD`；或配置术语表 |
