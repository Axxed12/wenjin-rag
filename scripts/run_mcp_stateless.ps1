param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ServerArgs
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot "src"
uv run --project (Join-Path $projectRoot "mcp-stateless") --locked `
    python -m wenjin.mcp.stateless_server @ServerArgs
exit $LASTEXITCODE
