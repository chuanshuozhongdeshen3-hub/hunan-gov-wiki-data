# BGE索引与完整材料章节召回交付说明

## 替换文件

将交付包中的文件放到项目根目录，并保持原有相对路径：

- `build_rag_index.py`
- `search_rag.py`
- `rag_common.py`
- `requirements-rag.txt`
- `build_bge_index.ps1`
- `RAG_README.md`
- `tests/test_rag.py`

本次核心改动位于 `search_rag.py`。`build_rag_index.py` 和 `rag_common.py`
一并提供，用于保证构建版本一致。

## Windows PowerShell运行

进入PyCharm项目根目录，激活原有虚拟环境，然后执行：

```powershell
pip install -r .\requirements-rag.txt

python .\tests\test_rag.py -v

.\build_bge_index.ps1 -Device cpu -BatchSize 16
```

如果已经正确安装CUDA版PyTorch，并且
`python -c "import torch; print(torch.cuda.is_available())"` 输出 `True`：

```powershell
.\build_bge_index.ps1 -Device cuda -BatchSize 32
```

`build_bge_index.ps1` 使用 `--force`，会替换现有的 `gov-wiki\rag`。

## Linux手动运行

```bash
python3 -m pip install -r requirements-rag.txt

python3 build_rag_index.py \
  --wiki gov-wiki/wiki \
  --catalog gov-wiki/wiki/_meta/rag_catalog_enriched.jsonl \
  --output gov-wiki/rag \
  --max-chars 600 \
  --overlap 80 \
  --model BAAI/bge-small-zh-v1.5 \
  --batch-size 16 \
  --device cpu \
  --force
```

## 预期构建结果

`gov-wiki/rag/index_manifest.json` 应满足：

```text
document_count = 12147
chunk_count = 116152
max_chunk_chars <= 600
embedding_model = BAAI/bge-small-zh-v1.5
embedding_shape = [116152, 512]
skipped = []
```

CPU与CUDA生成的向量可能存在极小浮点差异，不影响上述结构和检索逻辑。

## 完整材料召回验收

普通事项：

```powershell
python .\search_rag.py `
  --query "办理侨眷身份认定需要哪些材料" `
  --index .\gov-wiki\rag `
  --top-pages 2 `
  --pretty
```

预期：

- `retrieval.complete_section_recall.complete` 为 `true`；
- `doc_id` 为 `service:-3af984CTh-cb62cVHwV-A`；
- `chunk_count` 为 `8`；
- 返回第1至第8项全部申请材料。

“一件事”地区事项：

```powershell
python .\search_rag.py `
  --query "芙蓉区新生儿出生一件事需要什么材料" `
  --index .\gov-wiki\rag `
  --top-pages 2 `
  --pretty
```

预期：

- 第一个页面是芙蓉区地区实施页；
- 第二个页面是对应业务版本；
- `retrieval.complete_section_recall.complete` 为 `true`；
- 完整材料目标为
  `onething-variant:43PCN0009:fc31e4d2b72aeeff`；
- 业务版本返回14个材料切片。

## 运行后提供的检查文件

请提供：

1. `gov-wiki/rag/index_manifest.json`
2. `gov-wiki/rag/verification/service-materials.json`
3. `gov-wiki/rag/verification/onething-materials.json`

`gov-wiki/rag/` 已被 `.gitignore` 忽略，不需要上传整个向量索引到GitHub。
