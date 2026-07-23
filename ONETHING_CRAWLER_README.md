# “高效办成一件事”补抓程序

`supplement_onething_guides.py` 用于补抓现有58个“一件事”主题的地区实施清单和
完整办事指南。脚本读取已有的：

```text
gov-wiki/audit/search_only_candidates.json
```

因此不需要手工维护主题编码清单，也不会修改已经完成的普通事项数据。

## 1. 在 PyCharm Terminal 中预览

打开项目根目录，确认终端类型为 Windows PowerShell：

```powershell
cd D:\你的路径\hunan-gov-wiki-data
python .\supplement_onething_guides.py --dry-run
```

正常情况下应显示“可用主题：58”。

## 2. 推荐的样本测试

先抓“新生儿出生一件事”，最多保存两份地区指南：

```powershell
python .\supplement_onething_guides.py `
  --theme-code 43PCN0009 `
  --max-guides 2
```

检查汇总结果：

```powershell
$catalog = Get-Content .\gov-wiki\catalog\onething_guides.json -Raw | ConvertFrom-Json
$catalog | Select-Object status, catalog_theme_count, implementation_count, unique_guide_count
$catalog.themes | Select-Object theme_code, theme_title, status, implementation_count | Format-Table
```

同时抽查以下目录中的原始响应：

```text
gov-wiki/raw/onething/area_lists/
gov-wiki/raw/onething/implementations/
gov-wiki/raw/onething/guides/
```

## 3. 正式抓取58个主题

样本确认无误后运行：

```powershell
python .\supplement_onething_guides.py
```

默认行为：

- 请求间隔为 1.0 秒，再增加 0～0.5 秒随机延迟；
- 串行请求，不会并发压迫官网；
- 自动跳过 `approveId=0` 的无可用指南地区；
- 单个地区失败时记录错误，并继续处理其他地区；
- 原始响应写入缓存，中断后执行同一命令即可续跑；
- 单独重抓某个主题时，汇总目录会保留其他主题，不会被覆盖。

全量运行可能持续较长时间。终端可以使用 `Ctrl+C` 安全中断，已经写入的 JSON
不会丢失。

## 4. 断点续跑和刷新

直接重复原命令会优先使用缓存：

```powershell
python .\supplement_onething_guides.py
```

只重抓一个主题的完整指南，但保留地区树和实施清单缓存：

```powershell
python .\supplement_onething_guides.py `
  --theme-code 43PCN0009 `
  --refresh-guides
```

刷新该主题的所有接口：

```powershell
python .\supplement_onething_guides.py `
  --theme-code 43PCN0009 `
  --refresh-all
```

`--include-zero-approve` 会继续进入官网标记为零可用指南的地区，适合最后做完整性
复核，但请求量会显著增加，第一次正式抓取不建议开启。

## 5. 输出文件

```text
gov-wiki/
├─ raw/onething/
│  ├─ area_lists/<主题编码>/level_<层级>/parent_<地区ID>.json
│  ├─ implementations/<主题编码>/<地区编码>.json
│  └─ guides/<主题编码>/<onethingChecklistId>.json
├─ catalog/onething_guides.json
├─ reports/onething_latest_run.json
└─ state/onething_checkpoint.json
```

- `area_lists`：行政区划树原始响应；
- `implementations`：地区与 `onethingChecklistId` 的对应关系；
- `guides`：材料、条件、流程、联办事项、地点、附件和常见问题；
- `onething_guides.json`：供下一步 WIKI 转换使用的结构化总目录；
- `onething_latest_run.json`：本次错误、警告、请求数和缓存命中数；
- `onething_checkpoint.json`：最近进度。

如果终端最终显示 `completed_with_errors` 并返回退出码2，先查看运行报告，然后直接
重复执行。成功缓存会被复用，只会继续请求缺失或失败的内容。

## 6. 下一步

该脚本只保存官网原始数据，不修改 WIKI。补抓完成并检查报告后，再运行地区版
“一件事”WIKI转换程序，随后重新执行 DeepSeek 检索增强和 RAG 索引构建。
