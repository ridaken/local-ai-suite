param(
    [switch]$Apply,
    [string]$LegacyDb = "",
    [string]$StateDir = "",
    [string]$SecretDir = "config/secrets"
)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Install requirements first."
}
$arguments = @("-m", "mcp_gateway.migrate_v02", "--secret-dir", $SecretDir)
if ($LegacyDb) { $arguments += @("--legacy-db", $LegacyDb) }
if ($StateDir) { $arguments += @("--state-dir", $StateDir) }
if ($Apply) { $arguments += "--apply" }
& $python @arguments
