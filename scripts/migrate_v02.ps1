param(
    [switch]$Apply,
    [string]$LegacyDb = "",
    [string]$StateDir = "",
    [string]$SecretDir = "config/secrets"
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Install requirements first."
}
$envFile = Join-Path $repoRoot "config/.env"
if ((-not $LegacyDb -or -not $StateDir) -and -not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw "Configuration file not found. Copy config/.env.example to config/.env first."
}

if (-not $LegacyDb -or -not $StateDir) {
    Push-Location $repoRoot
    try {
        $resolvedJson = & docker compose --env-file $envFile -f docker-compose.yml config --format json
        if ($LASTEXITCODE -ne 0) { throw "Compose configuration validation failed." }
        $resolved = ($resolvedJson -join "`n") | ConvertFrom-Json
        $volumes = @($resolved.services."state-init".volumes)
        if (-not $LegacyDb) {
            $corpus = $volumes | Where-Object { $_.target -eq "/corpus" } | Select-Object -First 1
            if (-not $corpus) { throw "Could not resolve the Compose corpus mount." }
            $LegacyDb = Join-Path $corpus.source "settings.db"
        }
        if (-not $StateDir) {
            $state = $volumes | Where-Object { $_.target -eq "/state" } | Select-Object -First 1
            if (-not $state) { throw "Could not resolve the Compose state mount." }
            $StateDir = $state.source
        }
    }
    finally {
        Pop-Location
    }
}

$arguments = @("-m", "mcp_gateway.migrate_v02", "--secret-dir", $SecretDir)
$arguments += @("--legacy-db", $LegacyDb, "--state-dir", $StateDir)
if ($Apply) { $arguments += "--apply" }
Write-Host "Legacy database: $LegacyDb"
Write-Host "State directory: $StateDir"
Push-Location $repoRoot
try {
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
