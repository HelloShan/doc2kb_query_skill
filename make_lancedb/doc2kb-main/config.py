"""
doc2kb — 中心化配置模块
==========================
所有可调参数集中在此文件，但实际取值优先从项目根目录的 .env 文件读取
（参考 .env.example），不用改这个 .py 文件本身就能调整配置。

.env 不提交到 GitHub（已加入 .gitignore），每个人自己的部署环境维护
自己的一份，仓库里只保留 .env.example 作为样例/文档。

这里的值是"构建 + 查询两端都必须一致"的知识库身份参数（数据库路径、
表名、embedding 模型/维度）——只查询相关、不影响构建的参数
（TOP_K、相似度阈值、HOST/PORT、访问口令等）不在这里，
在 query_reference/ 目录下有自己独立的一份 .env，互不干扰。
"""

import os
import warnings
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# 从项目根目录（本文件所在目录）加载 .env，不依赖运行时的当前工作目录
load_dotenv(Path(__file__).resolve().parent / ".env")


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        print(f"[WARN] 环境变量 {name}={val!r} 不是合法整数，使用默认值 {default}")
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        print(f"[WARN] 环境变量 {name}={val!r} 不是合法数字，使用默认值 {default}")
        return default


# HuggingFace 镜像源：国内网络访问 huggingface.co 经常不稳定/连不上，
# 默认用镜像站。用 setdefault 而不是直接赋值——如果用户在 .env 或系统
# 环境变量里已经设置了 HF_ENDPOINT（比如海外服务器想用官方源），不会被
# 这里强制覆盖掉。
os.environ.setdefault("HF_ENDPOINT", os.getenv("DOC2KB_HF_ENDPOINT", "https://hf-mirror.com"))

# 一旦本地已经有 Docling/HuggingFace 模型缓存，可以打开这个开关跳过所有
# 联网检查。docx/pdf 每次起 Docling 转换时，huggingface_hub 都会尝试连一次
# 网核实模型版本/缓存是否最新——如果这台机器访问 hf-mirror.com（或配置的
# 官方源）不稳定甚至连不上（公司网络、代理、防火墙都可能导致这种情况，
# 而且可能是间歇性的，之前几百个文件能成功不代表现在依然能连上），
# 每一次这样的联网检查都可能顶着系统 TCP 超时（往往长达几十秒到几分钟）
# 卡住——如果同一时刻有几个 worker 线程恰好都在处理 docx/pdf，会看起来
# 像是"全部一起卡死"，跟真正的死循环卡死表现几乎一样，但根因完全不同
# （一个是代码 bug，一个是网络问题），需要分开排查。
# 打开这个开关后，Docling/huggingface_hub 完全依赖本地缓存，不再发起任何
# 网络请求：网络好不好都不会等，缓存有就用，缓存没有就直接报错（不会卡住），
# 适合"模型已经确认下载过、只是想安安静静把剩下的文件跑完"的场景。
DOC2KB_HF_OFFLINE = _env_bool("DOC2KB_HF_OFFLINE", False)
if DOC2KB_HF_OFFLINE:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# ============================================================
# 抑制第三方库的无效告警
# ============================================================

# Pillow：图片无法处理时不告警（直接丢弃）
warnings.filterwarnings("ignore", category=UserWarning, module="PIL")
warnings.filterwarnings("ignore", message=".*cannot write mode CMYK.*")
warnings.filterwarnings("ignore", message=".*cannot identify image file.*")

# Docling & python-docx：图片/VML/无LibreOffice 等噪音
logging.getLogger("docling").setLevel(logging.ERROR)
logging.getLogger("docling.datamodel").setLevel(logging.ERROR)
logging.getLogger("docling.backend").setLevel(logging.ERROR)
logging.getLogger("docling.pipeline").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*VML image cannot be found.*")
warnings.filterwarnings("ignore", message=".*Found DrawingML elements.*")
warnings.filterwarnings("ignore", message=".*LibreOffice.*")

# ============================================================
# 1. 目录路径
# ============================================================

# 原始文档目录 A：存放 docx/pdf/md 等源文件
SOURCE_DIR = Path(os.getenv("DOC2KB_SOURCE_DIR", "../source_docs"))

# MD 目标文件目录 B：转换后的 .md 文件存放位置
OUTPUT_MD_DIR = Path(os.getenv("DOC2KB_OUTPUT_MD_DIR", "../output_md"))

# LanceDB 知识库路径 C：向量数据库存储位置
DB_PATH = Path(os.getenv("DOC2KB_DB_PATH", "../doc2kb.lancedb"))

# 流水线状态文件
STATE_FILE = Path(os.getenv("DOC2KB_STATE_FILE", "../pipeline_state.json"))

# 日志文件（默认按日期自动生成，如 pipeline_20260628.log）
_DEFAULT_LOG = f"pipeline_{datetime.now().strftime('%Y%m%d')}.log"
LOG_FILE = os.getenv("DOC2KB_LOG_FILE", _DEFAULT_LOG)

# ============================================================
# 2. 文档转换配置
# ============================================================

# 支持转换的源文件扩展名（小写）
# 6 种核心文档格式 + 用于"非文档类"知识（SQL 建表脚本/YAML/JSON/INI 配置文件）的格式
DOC_EXTENSIONS = {".docx", ".md", ".pdf", ".txt", ".pptx", ".xlsx"}
CODE_CONFIG_EXTENSIONS = {".sql", ".yaml", ".yml", ".json", ".ini", ".conf", ".toml"}
SUPPORTED_EXTENSIONS = DOC_EXTENSIONS | CODE_CONFIG_EXTENSIONS

# convert.py 把 SQL/YAML/JSON/INI 等文件也统一转换成 .md 存放，
# 但分块阶段需要知道"这原本是什么类型"才能选择语义切分策略（按 SQL 语句/
# YAML 顶层 key/INI section 切，而不是无脑按字符数切）。
# 转换时会在输出 .md 文件首行写入这个标记；HTML 注释在 Markdown 里不可见，
# 不影响直接阅读转换后的文件。
SOURCE_EXT_MARKER_PREFIX = "<!-- doc2kb:source_ext="

# 转换引擎：Docling 为主力（支持 docx/pdf/xlsx/pptx），原生解析为降级
# Docling 提供表格还原、多栏识别、页眉页脚剥离等高级能力
CONVERT_ENGINE = os.getenv("DOC2KB_CONVERT_ENGINE", "docling")

# 生成 MD 文件超过此大小（KB）时打印警告
MAX_MD_FILE_SIZE_KB = _env_int("DOC2KB_MAX_MD_FILE_SIZE_KB", 500)

# ============================================================
# 3. RAG / 分块配置
# ============================================================

# Embedding 模型（fastembed，CPU运行）
# 备选: "BAAI/bge-base-zh-v1.5" 在这版 fastembed 里其实不支持（实测确认过），
# 中文系列目前只有 bge-small-zh-v1.5 能直接用；"jinaai/jina-embeddings-v2-base-zh"
# (768维, 最长8192token) 是支持长文本/中英混排的一个可用选项。
EMBEDDING_MODEL = os.getenv("DOC2KB_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

# 当前模型的真实最大序列长度（token 数，非字符数）。
# bge-small-zh-v1.5 上限 512 token；jina-embeddings-v2-base-zh 上限 8192 token。
# 换模型时必须同步修改此值，否则超出部分会在向量化时被模型静默截断，
# 导致长 chunk 尾部内容实际上没有参与向量化（检索召回率下降且不易察觉）。
EMBEDDING_MAX_TOKENS = _env_int("DOC2KB_EMBEDDING_MAX_TOKENS", 512)

# 向量维度，必须和 EMBEDDING_MODEL 的实际输出维度一致，否则写入 LanceDB
# 时会报 "cannot cast field 'vector'" 的维度不匹配错误。换模型时如果维度
# 也变了（比如从 512 维换到 768 维），必须同步改这里，并且删掉旧的
# LanceDB 数据库目录重新建表（`python doc_pipeline.py build --full`），
# 旧表的 schema 不会因为改了这个配置就自动变。
VECTOR_DIM = _env_int("DOC2KB_VECTOR_DIM", 512)

# Chunk 大小与重叠（字符数）
# 注意：这是 Markdown 分块用的"字符数"，不等于上面的 token 数。
# 中文在常见 BPE 分词器下 1 字符大致对应 1~1.5 token，1200 字符的 chunk
# 有较大概率超过 512 token 的模型上限。若换用支持更长上下文的模型可以放宽此值，
# 使用 bge-small 系列时建议保持在 400~500 字符更安全。
CHUNK_SIZE = _env_int("DOC2KB_CHUNK_SIZE", 1200)         # 每块最大字符数
CHUNK_OVERLAP = _env_int("DOC2KB_CHUNK_OVERLAP", 300)    # 块之间重叠字符数

# LanceDB 表名
TABLE_NAME = os.getenv("DOC2KB_TABLE_NAME", "docs")

# 术语表（可选）：人工维护的缩写/术语 -> 全称映射，用于查询前的术语展开，
# 缓解"问题里只有缩写、知识库原文用的是全称"（或反过来）导致的召回不足。
# 文件不存在时功能自动跳过，不影响其它流程。格式见 glossary.example.yaml。
GLOSSARY_PATH = Path(os.getenv("DOC2KB_GLOSSARY_PATH", "../glossary.yaml"))

# ============================================================
# 4. 并发与内存控制
# ============================================================

# 转换阶段的并行线程数
CONVERT_WORKERS = _env_int("DOC2KB_CONVERT_WORKERS", 4)

# Docling（docx/pdf 的高质量转换引擎）单独的并发上限，独立于 CONVERT_WORKERS。
# ────────────────────────────────────────────────────────────────────
# 重要：Docling 每次转换都会在自己的子进程里重新加载一整套模型（版面分析 +
# TableFormer 表格识别，装了 OCR 的话还有 OCR 模型），单个实例常规也要
# 占用 1GB+ 内存，处理大图片/复杂表格时峰值会更高。CONVERT_WORKERS 控制的
# 是"总并发线程数"，但 xlsx/pptx/txt 这些走原生解析的文件很轻量，同时跑
# 4 个完全没问题；docx/pdf 一旦同时凑够 CONVERT_WORKERS 个都在跑 Docling，
# 相当于同时把好几份完整的模型塞进内存，很容易在处理大文件/复杂表格时
# 直接把系统内存挤爆——实测会看到 `std::bad_alloc`、
# `numpy.core._exceptions._ArrayMemoryError`、甚至进程被系统直接杀掉
# （Windows 下是 exitcode 3221225477 / 0xC0000005 access violation），
# 表现为整台电脑卡死、显示驱动因资源耗尽触发重置（黑屏闪烁）——这不是
# "内存泄漏"（该释放的没释放），而是短时间内并发加载的模型实例太多，
# 瞬时内存需求超过了系统能提供的量。
#
# 这个值默认给得很保守（1，即 docx/pdf 全部排队、一个一个走 Docling），
# 用来保证"不管 CONVERT_WORKERS 开多大，都不会因为 Docling 并发而把机器
# 打死"。如果你的机器内存充足（比如 32GB+）、处理的文档也不算特别大，
# 可以把这个值调到 2 观察一下内存占用再决定要不要继续调大；xlsx/pptx/txt
# 等原生解析路径不受这个限制，仍然按 CONVERT_WORKERS 的并发跑。
DOCLING_MAX_CONCURRENT = _env_int("DOC2KB_DOCLING_MAX_CONCURRENT", 1)

# 入库阶段的 batch 大小（每批处理的文本数）
EMBED_BATCH_SIZE = _env_int("DOC2KB_EMBED_BATCH_SIZE", 32)

# 每处理 N 个文件后触发一次 Python 层 GC（对 LanceDB/onnxruntime 这些
# 原生扩展占用的内存没有实质性帮助，详见 ingest.py 里的说明）
DB_FLUSH_INTERVAL = _env_int("DOC2KB_DB_FLUSH_INTERVAL", 20)

# 跳过超大文件（超过此 MB 的文件跳过转换）
LARGE_FILE_THRESHOLD_MB = _env_int("DOC2KB_LARGE_FILE_THRESHOLD_MB", 50)

# 单文件转换/入库超时秒数（防止卡死的文档拖死整个流水线）
CONVERT_TIMEOUT = _env_int("DOC2KB_CONVERT_TIMEOUT", 600)

# ============================================================
# 5. 乱码检测配置
# ============================================================

# 前 N 字节中需要包含的 CJK 标点数量（≥此值视为正常文档）
GARBLED_PUNCT_THRESHOLD = _env_int("DOC2KB_GARBLED_PUNCT_THRESHOLD", 3)

# 检查文件的头部字节数
GARBLED_CHECK_BYTES = _env_int("DOC2KB_GARBLED_CHECK_BYTES", 2000)

# ============================================================
# 6. 输出格式
# ============================================================

# 是否在终端输出带颜色的日志
COLOR_OUTPUT = _env_bool("DOC2KB_COLOR_OUTPUT", True)


def validate_config():
    """检查配置合理性，打印警告"""
    if CONVERT_WORKERS < 1 or CONVERT_WORKERS > 16:
        print(f"[WARN] CONVERT_WORKERS={CONVERT_WORKERS}，推荐 1-16")
    if DB_FLUSH_INTERVAL < 5:
        print(f"[WARN] DB_FLUSH_INTERVAL={DB_FLUSH_INTERVAL} 过小，可能影响性能")
    if CHUNK_SIZE < CHUNK_OVERLAP * 2:
        print(f"[WARN] CHUNK_SIZE ({CHUNK_SIZE}) 应至少为 CHUNK_OVERLAP ({CHUNK_OVERLAP}) 的两倍")
    # 粗略提醒：中文场景下 1 字符约等于 1~1.5 token，这里只按 1 倍做保守提醒
    # （启发式提醒，不代表精确 token 数；精确值需用 fastembed 的 token_count 计算）
    if CHUNK_SIZE > EMBEDDING_MAX_TOKENS:
        print(f"[WARN] CHUNK_SIZE ({CHUNK_SIZE} 字符) 超过 EMBEDDING_MAX_TOKENS ({EMBEDDING_MAX_TOKENS} token)，"
              f"中文文本大概率会在向量化时被模型静默截断，chunk 尾部内容可能不参与检索。"
              f"建议调低 CHUNK_SIZE 或更换为支持更长上下文的模型（如 jina-embeddings-v2-base-zh）。")


# 首次导入时自动验证
validate_config()
