param(
    [string]$Python = "python",
    [string]$Index = ".\gov-wiki\rag"
)

$ErrorActionPreference = "Stop"
$Manifest = Join-Path $Index "index_manifest.json"
$Verification = Join-Path $Index "verification"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path $Manifest)) {
    throw "RAG index manifest not found: $Manifest"
}

New-Item -ItemType Directory -Force -Path $Verification | Out-Null

function Invoke-RagCheck {
    param(
        [string]$Question,
        [string]$OutputPath,
        [int]$TopPages = 2
    )

    $Result = & $Python .\search_rag.py `
        --query $Question `
        --index $Index `
        --top-pages $TopPages `
        --no-vector `
        --pretty
    $SearchExitCode = $LASTEXITCODE
    if ($SearchExitCode -ne 0) {
        throw "Retrieval test failed with exit code: $SearchExitCode"
    }

    $JsonText = ($Result -join [Environment]::NewLine) + [Environment]::NewLine
    [System.IO.File]::WriteAllText($OutputPath, $JsonText, $Utf8NoBom)
    return ($JsonText | ConvertFrom-Json)
}

$ServicePath = Join-Path $Verification "service-materials.json"
$Service = Invoke-RagCheck `
    -Question "办理侨眷身份认定需要哪些材料" `
    -OutputPath $ServicePath

if ($Service.pages.Count -ne 1) {
    throw "Expected exactly one ordinary-service page; got $($Service.pages.Count)"
}
if ($Service.pages[0].title -ne "侨眷身份认定") {
    throw "Unexpected ordinary-service result: $($Service.pages[0].title)"
}
if ($Service.retrieval.complete_section_recall.chunk_count -ne 8) {
    throw "Expected 8 complete material chunks for the ordinary service"
}
if ($Service.pages[0].PSObject.Properties.Name -contains "official_url") {
    throw "official_url should be hidden for a normal material query"
}

$OneThingPath = Join-Path $Verification "onething-materials.json"
$OneThing = Invoke-RagCheck `
    -Question "芙蓉区新生儿出生一件事需要什么材料" `
    -OutputPath $OneThingPath

if ($OneThing.pages.Count -ne 2) {
    throw "Expected region and variant pages; got $($OneThing.pages.Count)"
}
if ($OneThing.pages[0].page_type -ne "theme_region") {
    throw "The first OneThing page should be theme_region"
}
if ($OneThing.pages[1].page_type -ne "theme_variant") {
    throw "The second OneThing page should be theme_variant"
}
if ($OneThing.retrieval.complete_section_recall.chunk_count -ne 14) {
    throw "Expected 14 complete material chunks for the OneThing variant"
}
if ($OneThing.pages[0].PSObject.Properties.Name -contains "official_url") {
    throw "official_url should be hidden from the regional result"
}
if ($OneThing.pages[1].PSObject.Properties.Name -contains "official_url") {
    throw "official_url should be hidden from the variant result"
}

$UrlPath = Join-Path $Verification "service-url.json"
$UrlResult = Invoke-RagCheck `
    -Question "侨眷身份认定的官网链接是什么" `
    -OutputPath $UrlPath `
    -TopPages 1

if (-not ($UrlResult.pages[0].PSObject.Properties.Name -contains "official_url")) {
    throw "An explicit link query should include official_url"
}

Write-Host "RAG output-rule verification passed."
Write-Host "Ordinary materials: one target page, 8 complete chunks, URL hidden."
Write-Host "OneThing materials: region + variant, 14 complete chunks, URLs hidden."
Write-Host "Explicit link query: official_url included."
Write-Host "UTF-8 results:"
Write-Host "1. $ServicePath"
Write-Host "2. $OneThingPath"
Write-Host "3. $UrlPath"
