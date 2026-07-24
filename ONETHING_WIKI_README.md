# “高效办成一件事”WIKI转换与去重

补抓完成后，`onething_json_to_wiki.py` 会把地区指南转换为三层WIKI：

```text
主题总览
├─ 去重后的业务版本：材料、条件、流程、联办事项、常见问题
└─ 地区实施页：地区、部门、电话、地点、办公时间、对应版本
```

程序通过官方业务字段的标准化内容计算SHA-256指纹，不使用大模型判断两个地区
是否相同。地区名称、部门、电话和办理地点不会参与业务版本指纹。

当前数据的实际结果：

- 58个主题；
- 7,870份地区实施指南；
- 去重后827个业务版本；
- 其中109个版本覆盖至少5个地区；
- 加上52个存在指南的主题总览，共161个DeepSeek重点增强页面。

其余地区页和少量地区特有版本仍会进入关键词和向量索引，只是不重复调用
DeepSeek生成摘要。

## 1. 单主题样本转换

在PyCharm的Windows PowerShell终端运行：

```powershell
cd D:\你的路径\hunan-gov-wiki-data

python .\onething_json_to_wiki.py `
  --input .\gov-wiki `
  --output .\gov-wiki\wiki-onething-sample `
  --theme-code 43PCN0009
```

检查：

```powershell
$m = Get-Content .\gov-wiki\wiki-onething-sample\_meta\onething_generated_manifest.json -Raw |
  ConvertFrom-Json

$m |
  Select-Object theme_count, variant_count, region_page_count,
    llm_enrichment_priority_count, skipped_count
```

## 2. 正式生成完整WIKI

正式转换仍使用原来的统一入口：

```powershell
python .\json_to_wiki.py `
  --input .\gov-wiki `
  --output .\gov-wiki\wiki
```

该命令会同时生成普通事项和“一件事”，并将它们合并到：

```text
gov-wiki/wiki/_meta/rag_catalog.jsonl
```

转换后检查：

```powershell
$r = Get-Content .\gov-wiki\wiki\_meta\conversion_report.json -Raw |
  ConvertFrom-Json

$r |
  Select-Object converted_service_pages, converted_theme_pages,
    onething_variant_pages, onething_region_pages,
    onething_llm_enrichment_priority_count, onething_skipped_count,
    rag_record_count

$r.lint |
  Select-Object broken_wikilink_count, frontmatter_error_count,
    entity_pages_missing_from_subindexes_count
```

完整结果应至少满足：

```text
onething_variant_pages = 827
onething_region_pages = 7870
onething_llm_enrichment_priority_count = 161
onething_skipped_count = 0
broken_wikilink_count = 0
frontmatter_error_count = 0
```

## 3. DeepSeek只增强重点页面

先查看一条实际请求，不调用API：

```powershell
python .\enrich_rag_catalog.py --only-flagged --dry-run
```

用5个新页面测试：

```powershell
python .\enrich_rag_catalog.py --only-flagged --limit 5
```

确认输出后处理全部重点页面：

```powershell
python .\enrich_rag_catalog.py --only-flagged
```

`--only-flagged` 的作用：

- 复用旧 `rag_catalog_enriched.jsonl` 中普通事项的DeepSeek增强结果；
- 处理52个存在指南的主题总览；
- 处理覆盖至少5个地区的109个主要业务版本；
- 跳过7,870个地区页和718个少量地区特有版本的API调用；
- 被跳过页面仍完整保留在最终RAG目录中。

如果希望扩大DeepSeek覆盖范围，可以在转换时调整阈值。例如覆盖至少2个地区的
版本都进行增强：

```powershell
python .\json_to_wiki.py --onething-enrich-min-areas 2
```

默认值5在成本、覆盖率和重复度之间更合适。

## 4. 重新建立RAG索引

先用FTS验证，不下载或加载BGE：

```powershell
python .\build_rag_index.py --skip-embeddings --force

python .\search_rag.py `
  --query "芙蓉区新生儿出生一件事需要什么材料" `
  --no-vector `
  --top-pages 2 `
  --pretty
```

地区详细问题会强制配对返回：

1. 对应地区实施页；
2. 该地区对应的业务版本页。

正式建立BGE向量索引：

```powershell
python .\build_rag_index.py `
  --force `
  --model "BAAI/bge-small-zh-v1.5" `
  --batch-size 32
```

CPU环境可以使用：

```powershell
python .\build_rag_index.py `
  --force `
  --model "BAAI/bge-small-zh-v1.5" `
  --batch-size 8 `
  --device cpu
```

所有正文切片和Embedding输入仍严格限制为最多600字符。

