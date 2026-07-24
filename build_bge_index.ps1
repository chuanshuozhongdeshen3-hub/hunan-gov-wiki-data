param(
    [string]$Python = "python",
    [string]$Model = "BAAI/bge-small-zh-v1.5",
    [int]$BatchSize = 16,
    [ValidateSet("cpu", "cuda")]
    [string]$Device = "cpu"
)

$ErrorActionPreference = "Stop"

$Wiki = ".\gov-wiki\wiki"
$Catalog = ".\gov-wiki\wiki\_meta\rag_catalog_enriched.jsonl"
$Index = ".\gov-wiki\rag"
$Manifest = Join-Path $Index "index_manifest.json"
$Embeddings = Join-Path $Index "embeddings.npy"
$Verification = Join-Path $Index "verification"

if (-not (Test-Path $Catalog)) {
    throw "Catalog file not found: $Catalog"
}

Write-Host "Starting BGE hybrid index build. --force will replace the existing index at $Index"
& $Python .\build_rag_index.py `
    --wiki $Wiki `
    --catalog $Catalog `
    --output $Index `
    --max-chars 600 `
    --overlap 80 `
    --model $Model `
    --batch-size $BatchSize `
    --device $Device `
    --force

if ($LASTEXITCODE -ne 0) {
    throw "build_rag_index.py failed with exit code: $LASTEXITCODE"
}
if (-not (Test-Path $Manifest)) {
    throw "Build finished but manifest was not created: $Manifest"
}
if (-not (Test-Path $Embeddings)) {
    throw "Build finished but embeddings were not created: $Embeddings"
}

$Info = Get-Content $Manifest -Raw | ConvertFrom-Json
if ($Info.document_count -ne 12147) {
    throw "Unexpected RAG document count: $($Info.document_count); expected 12147"
}
if ($Info.max_chunk_chars -gt 600) {
    throw "A chunk exceeds 600 characters: $($Info.max_chunk_chars)"
}
if ($Info.skipped.Count -ne 0) {
    throw "The build skipped $($Info.skipped.Count) document(s)"
}
if (-not $Info.embedding_model) {
    throw "Manifest does not contain embedding_model; --skip-embeddings may have been used."
}

New-Item -ItemType Directory -Force -Path $Verification | Out-Null

Write-Host "Running standard service materials retrieval test..."
& $Python .\search_rag.py `
    --query "办理侨眷身份认定需要哪些材料" `
    --index $Index `
    --top-pages 2 `
    --pretty |
    Tee-Object -FilePath (Join-Path $Verification "service-materials.json")

if ($LASTEXITCODE -ne 0) {
    throw "Standard service retrieval test failed with exit code: $LASTEXITCODE"
}

Write-Host "Running OneThing regional materials retrieval test..."
& $Python .\search_rag.py `
    --query "芙蓉区新生儿出生一件事需要什么材料" `
    --index $Index `
    --top-pages 2 `
    --pretty |
    Tee-Object -FilePath (Join-Path $Verification "onething-materials.json")

if ($LASTEXITCODE -ne 0) {
    throw "OneThing retrieval test failed with exit code: $LASTEXITCODE"
}

Write-Host ""
Write-Host "BGE index build and retrieval tests completed."
$Info |
    Select-Object document_count, chunk_count, max_chunk_chars,
        embedding_model, embedding_shape, embedding_device,
        @{Name = "skipped_count"; Expression = { $_.skipped.Count }} |
    Format-List

Write-Host "Please review the following three files:"
Write-Host "1. $Manifest"
Write-Host "2. $(Join-Path $Verification 'service-materials.json')"
Write-Host "3. $(Join-Path $Verification 'onething-materials.json')"
