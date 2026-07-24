# RAG输出规则更新

本更新只改变检索结果的整理方式，不改变Wiki、SQLite关键词索引或BGE向量。
已有的 `gov-wiki/rag/` 可以继续使用，无需重新生成116,152条向量。

## 覆盖文件

将压缩包解压到项目根目录，覆盖：

- `search_rag.py`
- `build_bge_index.ps1`
- `RAG_README.md`
- `BGE_MATERIAL_RECALL_DELIVERY.md`
- `tests/test_rag.py`

新增：

- `verify_rag_output.ps1`

## 本次规则

1. 普通事项完整召回申请材料后，只返回目标事项，避免相似事项污染答案。
2. 地区“一件事”继续返回地区实施页和业务版本页。
3. 默认删除 `official_url`；明确询问官网、链接、网址或办事入口时才返回。
4. 可以用 `--include-urls` 强制保留链接，主要用于调试。
5. PowerShell验证结果统一保存为无BOM UTF-8 JSON。

## 本地运行

```powershell
python .\tests\test_rag.py -v

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\verify_rag_output.ps1
```

验证脚本使用现有索引，并添加 `--no-vector`，因此不会加载BGE模型，也不会重建
Embedding。成功后应显示：

```text
RAG output-rule verification passed.
Ordinary materials: one target page, 8 complete chunks, URL hidden.
OneThing materials: region + variant, 14 complete chunks, URLs hidden.
Explicit link query: official_url included.
```

生成的UTF-8测试文件位于：

```text
gov-wiki/rag/verification/service-materials.json
gov-wiki/rag/verification/onething-materials.json
gov-wiki/rag/verification/service-url.json
```
