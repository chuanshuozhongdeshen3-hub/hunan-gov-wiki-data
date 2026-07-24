$exclude = @(
    ".git",
    ".idea",
    ".venv",
    "__pycache__",
    "node_modules"
)

function Show-Tree {
    param(
        [string]$Path = ".",
        [string]$Prefix = ""
    )

    $items = Get-ChildItem -LiteralPath $Path -Force |
        Where-Object { $_.Name -notin $exclude } |
        Sort-Object @{Expression = { -not $_.PSIsContainer }}, Name

    for ($i = 0; $i -lt $items.Count; $i++) {
        $item = $items[$i]
        $isLast = $i -eq $items.Count - 1
        $branch = if ($isLast) { "\---" } else { "+---" }

        "$Prefix$branch$($item.Name)"

        if ($item.PSIsContainer) {
            $nextPrefix = if ($isLast) {
                "$Prefix    "
            } else {
                "$Prefix|   "
            }

            Show-Tree -Path $item.FullName -Prefix $nextPrefix
        }
    }
}

Split-Path -Leaf (Get-Location)
Show-Tree