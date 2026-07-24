# 湖南政务 Wiki RAG

“高效办成一件事”的地区实施指南补抓方法见
[`ONETHING_CRAWLER_README.md`](ONETHING_CRAWLER_README.md)。补抓完成并转换为
Wiki 后，再重新执行本文件中的检索增强和索引构建步骤。

本目录中的脚本完成三件事：

1. 可选地用 DeepSeek 为现有目录补充检索别名、常见问法和检索摘要；
2. 按 Markdown 章节切片，建立中文字符二元组 FTS5/BM25 索引和 BGE 向量；
3. 用标题/编码精确匹配、BM25 和向量召回融合，向 Hermes 返回约两篇 Wiki 内容。

Wiki 正文不会被 DeepSeek 修改。LLM 生成的内容只保存在检索目录中。

## 1. PowerShell 环境

在 PyCharm Terminal 中进入项目根目录：

```powershell
cd D:\你的路径\hunan-gov-wiki-data
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r .\requirements-rag.txt
```

如果 PowerShell 禁止激活脚本，可以只对当前终端放开：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 2. 先测试 600 字切片和关键词索引

第一次建议跳过模型下载，只验证切片和 BM25：

```powershell
python .\build_rag_index.py --skip-embeddings --force
python .\search_rag.py --query "船舶名称核准需要哪些材料" --no-vector --pretty
```

`gov-wiki\rag\index_manifest.json` 中的 `max_chunk_chars` 必须小于或等于 600。

## 3. 可选：DeepSeek 检索增强

API Key 只放在环境变量中，不要写进源码、`.env` 或 Git：

```powershell
$env:DEEPSEEK_API_KEY = "你的API Key"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"
```

模型名以 DeepSeek 当前账户和官方文档为准，也可以在命令行通过 `--model` 指定。先查看一条请求内容，不调用 API：

```powershell
python .\enrich_rag_catalog.py --dry-run
```

然后用 10 条做小样本：

```powershell
python .\enrich_rag_catalog.py --limit 10
```

检查下面三个文件：

- `gov-wiki\wiki\_meta\rag_catalog_enriched.jsonl`：增强后的目录；
- `gov-wiki\wiki\_meta\rag_catalog_enriched.cache.jsonl`：成功响应缓存；
- `gov-wiki\wiki\_meta\rag_catalog_enriched.report.json`：运行报告。

重复执行会使用缓存，不会再次收费。确认小样本后运行全部：

```powershell
python .\enrich_rag_catalog.py
```

中途中断也不会丢失已经完成的响应。脚本下一次运行会从缓存恢复。对输出进行抽样检查后，再建立正式索引。

## 4. 建立完整混合索引

脚本优先使用 `rag_catalog_enriched.jsonl`；若它不存在，则使用原始 `rag_catalog.jsonl`。

如果使用Windows PowerShell，可以直接运行包含构建后校验和两条检索测试的脚本：

```powershell
.\build_bge_index.ps1 -Device cpu -BatchSize 16
```

有可用的NVIDIA CUDA环境时：

```powershell
.\build_bge_index.ps1 -Device cuda -BatchSize 32
```

也可以手动执行：

```powershell
python .\build_rag_index.py --force --model "BAAI/bge-small-zh-v1.5" --batch-size 32
```

首次执行会从 Hugging Face 下载模型。显存不足或者只有 CPU 时可以减小批量：

```powershell
python .\build_rag_index.py --force --model "BAAI/bge-small-zh-v1.5" --batch-size 8 --device cpu
```

索引产物：

```text
gov-wiki/rag/
├─ documents.jsonl
├─ keyword_index.db
├─ embeddings.npy
└─ index_manifest.json
```

## 5. 检索测试

```powershell
python .\search_rag.py --query "身份证到期怎么换证" --top-pages 2 --pretty
python .\search_rag.py --query "开办食品经营企业需要什么材料" --top-pages 2 --pretty
```

当问题包含“材料、资料、证件、要带什么”等表达时，检索器会在选定事项后扩展
返回该事项的完整“申请材料”章节，而不再受 `--chunks-per-page` 限制。普通事项
若使用独立材料页，会自动返回该材料页；地区“一件事”会扩展其对应业务版本的
完整材料章节。输出中的 `retrieval.complete_section_recall.complete` 为 `true`
表示材料章节完整。

为避免小模型混淆相似事项，普通事项已经完整召回材料章节时只返回目标事项；
地区“一件事”仍返回地区实施页和业务版本页两篇。`official_url` 默认不出现在
结果中；问题明确包含“官网、链接、网址、办事入口”等表达时才返回。调试时也可
显式添加 `--include-urls`。

输出是 JSON，包含：

- 最相关的两篇 Wiki；
- 每篇最多三个相关片段；
- Wiki 相对路径；明确索要链接时才包含官网链接；
- `answerable` 安全标记；
- 给 Hermes 的回答约束。

当前有地区实施指南的“一件事”主题可正常回答；没有可用地区指南的主题会标记
为 `answerable: false`。回答模型只能说明未找到可用指南，不能推测办理条件或材料。

## 6. 转移到 Linux

在 PC 上完成索引后，将以下内容复制到 Linux 原生目录，例如 `/home/user/hunan-gov-wiki-data/`：

- `gov-wiki/wiki/`
- `gov-wiki/rag/`
- `rag_common.py`
- `search_rag.py`
- `requirements-rag.txt`
- 本地下载的 `BAAI/bge-small-zh-v1.5` 模型，或者在 Linux 上重新下载

文档向量可以在 PC 上生成，但 Linux 每次收到新问题时仍需使用同一个 BGE 模型生成问题向量。Hermes 应调用 `search_rag.py`，把返回的 `pages` 交给 Qwen2.5-3B，并遵守 `answer_policy`。

## 设计约束

- 每个 `text` 和 `embedding_text` 均硬限制为最多 600 个字符；
- Markdown 按标题和自然段切分，超长段落才使用 80 字重叠；
- 精确标题/事项编码权重大于 BM25 和向量相似度；
- 中文 BM25 使用字符二元组，不依赖 Jieba 词典；
- 文档与查询向量都做 L2 归一化，使用点积计算余弦相似度；
- DeepSeek 结果不进入官方 Wiki 正文。
