# doc2kb 📄→🧠

**文档→向量知识库统一流水线**

将原始文档自动转换为 Markdown（只处理文字，不做图片识别），再构建成 LanceDB 向量知识库（RAG 可检索）。
代码由deepseek v4完成。

---

## 🚀 快速开始

### 1. 安装依赖

```bash
uv pip install -r requirements.txt

# 默认 PDF 高质量转换（含布局分析）
uv pip install docling
```

### 2. 准备文档目录

```
source_docs/          ← 把你的文档/SQL/配置文件放这里 (docx/pdf/md/txt/pptx/xlsx/sql/yaml/json/ini)
output_md/            ← 自动生成的 MD 文件
doc2kb.lancedb/       ← 输出的向量知识库
doc2kb/               ← 代码目录
└── pipeline_20260628.log ← 自动生成的日期日志
```

### 3. 运行流水线

```bash
# 完整流水线（增量：只处理变更和失败的文件）
python doc_pipeline.py build

# 全量重建（清空知识库从头来过）
python doc_pipeline.py build --full

# 仅文档转换
python doc_pipeline.py build --convert-only

# 仅向量入库
python doc_pipeline.py build --ingest-only

# 重试失败文件
python doc_pipeline.py retry

# 查看状态 / 失败清单 / 知识库统计
python doc_pipeline.py status
python doc_pipeline.py list-failed
python doc_pipeline.py stats
```

---

## 🏗 架构

```
source_docs/ (docx/pdf/md/txt/pptx/xlsx/sql/yaml/json/ini)
     │
     ▼
┌─────────────────────────────────────┐
│         doc_pipeline.py              │
│  build / retry / status / list-*    │
├─────────────────────────────────────┤
│  convert.py ←→ validate.py ←→ ingest.py │
│       ↕             ↕             ↕      │
│  state.py (SHA256追踪 + JSON状态持久化)   │
│  config.py (中心化配置)                   │
└─────────────────────────────────────┘
     │                       │
     ▼                       ▼
  output_md/          doc2kb.lancedb/
  (清洁后的 MD)       (向量知识库)
```

### 模块说明

| 模块 | 职责 |
|------|------|
| **doc_pipeline.py** | CLI 入口，子命令分发，流水线编排 |
| **config.py** | 所有可调参数中心化，支持环境变量覆盖 |
| **state.py** | 每个文件的 SHA256 + 转换/入库状态追踪 |
| **validate.py** | 文档乱码检测 |
| **convert.py** | 6种文档格式 + SQL/YAML/JSON/INI等配置类格式 → MD 转换引擎 + 模板清洁 + xlsx空行过滤 |
| **ingest.py** | MD → 语义分块（文档按结构，SQL/YAML/JSON/INI 按语句/顶层key/section）→ 嵌入向量 → LanceDB 入库 |
| **glossary.py** | 可选术语表：查询前展开缩写/术语，缓解专业名词召回不足 |

---

## 🎯 核心功能

### 格式转换

**6 种核心文档格式：**

| 格式 | 转换器 | 说明 |
|------|--------|------|
| `.docx` | Docling → python-docx → ZIP回退 | 三级降级，损坏文件自动提取纯文本 |
| `.pdf` | Docling → pypdf | 高质量布局分析优先，损坏PDF友好报错 |
| `.xlsx` | openpyxl | **自动过滤全空/全零行** |
| `.pptx` | python-pptx | 每页幻灯片转为二级标题 |
| `.md` | 复制+清洁 | 保留所有格式 |
| `.txt` | **编码智能回退** | utf-8→gbk→gb2312→latin-1 |

**SQL 建表脚本 / 配置文件（不走文档清洗规则，按语义单元分块）：**

| 格式 | 分块策略 | 说明 |
|------|----------|------|
| `.sql` | 按语句边界（正确跳过字符串/注释里的 `;`） | 不会把一条 `CREATE TABLE` 从中间切断 |
| `.yaml` / `.yml` | 按顶层 key | 嵌套内容和注释保持完整 |
| `.json` | 按顶层 key | 解析失败时兜底为整体一块 |
| `.ini` / `.conf` / `.toml` | 按 `[section]` | |

这类文件的原始内容保持原样（不做文档去噪清洗，避免误删代码里的
注释行），每个 chunk 会带 `doc_type` 标记方便区分来源。

### xlsx 表格空行过滤

过滤三种垃圾行且**不破坏 `| --- |` 分割行**：

| 原始行 | 是否过滤 |
|--------|:--------:|
| `|  |  |  |`（全空） | ✅ |
| `| 0 | 0 | 0 |`（全零） | ✅ |
| `|  | 0 |  |`（混合空） | ✅ |
| `| 西瓜 | 3 | 15 |`（有效数据） | ❌ |

### 文件变更追踪

每个源文件记录 **SHA256** 哈希，文件修改后自动重新处理，旧版本向量自动从 LanceDB 删除。

### 乱码检测

PDF→MD 转换后自动检测，通过中文标点计数 + 模板匹配 + ASCII 单词兜底三重判断。

---

## ⚙️ 配置项（.env）

改配置直接编辑项目根目录的 `.env`（把 `.env.example` 复制一份改名即可），
不用碰 `config.py` 本身；`.env` 不会提交到 GitHub。

| 配置（.env 变量名） | 默认值 | 说明 |
|------|--------|------|
| `DOC2KB_SOURCE_DIR` | `../source_docs` | 源文档目录 |
| `DOC2KB_OUTPUT_MD_DIR` | `../output_md` | MD 输出目录 |
| `DOC2KB_DB_PATH` | `../doc2kb.lancedb` | LanceDB 路径 |
| `DOC2KB_LOG_FILE` | `pipeline_YYYYMMDD.log` | 日志文件（按日期） |
| `DOC2KB_CONVERT_WORKERS` | 4 | 并行线程数 |
| `DOC2KB_CHUNK_SIZE` | 1200 | 分块字符数 |
| `DOC2KB_CHUNK_OVERLAP` | 300 | 块重叠字符数 |
| `DOC2KB_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 嵌入模型 |
| `DOC2KB_VECTOR_DIM` | 512 | 向量维度，必须和模型实际输出维度一致 |
| `DOC2KB_LARGE_FILE_THRESHOLD_MB` | 50 | 跳过超大文件 |

完整变量列表见 `.env.example` 里的注释。

## 🔍 查询知识库

构建完知识库之后，怎么查询是另一部分——`query_reference/` 目录下有
一份查询服务的参考实现（不是可以被 Agent 直接安装使用的 skill 包，
只是查询逻辑的参考代码），包括自己独立的 `.env` 配置、混合检索、
访问口令鉴权等，详见 `query_reference/README.md`。

---

## 🪟 兼容性 & 最低配置

- **操作系统**: Windows 11 / Linux / macOS
- **CPU**: 4 线程
- **内存**: 2GB
- **依赖**: 纯 Python 包，无需系统级工具

---

## 🔄 典型工作流

```bash
# 日常增量更新：放入新文档 → 运行
python doc_pipeline.py build

# 全量重建
python doc_pipeline.py build --full

# 排查失败
python doc_pipeline.py status
python doc_pipeline.py list-failed
python doc_pipeline.py retry

# 分步调试
python doc_pipeline.py build --convert-only  # 只看转换
python doc_pipeline.py build --ingest-only   # 只看入库
```

---

## 📝 License

MIT
