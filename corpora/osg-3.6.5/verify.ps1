$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$projectRoot = (Resolve-Path (Join-Path $root '..\..')).Path
$manifest = Get-Content -Raw -LiteralPath (Join-Path $root 'source-manifest.json') | ConvertFrom-Json
$rawDataRoot = Join-Path $projectRoot 'raw_data'
$rawRoot = [System.IO.Path]::GetFullPath((Join-Path $rawDataRoot $manifest.raw_data_subdir))
$trustedPrefix = [System.IO.Path]::GetFullPath($rawDataRoot).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
if (!$rawRoot.StartsWith($trustedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'raw_data_subdir escapes raw_data root'
}
$known = @{}
foreach ($entry in $manifest.files) {
    $path = Join-Path $rawRoot $entry.path
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing: $($entry.path)" }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower()
    $expectedRawHash = if ($entry.raw_sha256) { $entry.raw_sha256 } else { $entry.sha256 }
    if ($hash -ne $expectedRawHash) { throw "Raw hash mismatch: $($entry.path)" }
    $known[$entry.path] = $entry
}
$seeds = @(Get-Content -LiteralPath (Join-Path $root 'dev-seed.jsonl') | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
$ids = @{}
$spanCount = 0
foreach ($seed in $seeds) {
    if ($ids.ContainsKey($seed.id)) { throw "Duplicate id: $($seed.id)" }
    $ids[$seed.id] = $true
    if ($seed.version -ne $manifest.version) { throw "Version mismatch: $($seed.id)" }
    foreach ($span in $seed.evidence) {
        if (!$known.ContainsKey($span.path) -or !$known[$span.path].indexable) { throw "Evidence not indexable: $($span.path)" }
        $lines = @(Get-Content -LiteralPath (Join-Path $rawRoot $span.path))
        if ($span.start_line -lt 1 -or $span.end_line -lt $span.start_line -or $span.end_line -gt $lines.Count) {
            throw "Bad evidence range: $($seed.id)"
        }
        $spanCount++
    }
}
[pscustomobject]@{
    status = 'PASS'
    version = $manifest.version
    files = $manifest.files.Count
    indexable_files = @($manifest.files | Where-Object indexable).Count
    dev_questions = $seeds.Count
    evidence_spans = $spanCount
    bytes = ($manifest.files | Measure-Object bytes -Sum).Sum
    note = 'Source integrity and evidence-range check only; not a retrieval or compile test.'
} | ConvertTo-Json
