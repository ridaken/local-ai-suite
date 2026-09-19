param(
    [switch]$ApplyMigration,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$envFile = Join-Path $repoRoot "config/.env"
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw "Configuration file not found. Copy config/.env.example to config/.env and set the storage paths first."
}

Push-Location $repoRoot
try {
    function Invoke-SetupScript {
        param(
            [Parameter(Mandatory = $true)][string]$Path,
            [string[]]$Arguments = @()
        )

        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Setup command failed with exit code $LASTEXITCODE`: $Path"
        }
    }

    Invoke-SetupScript -Path (Join-Path $PSScriptRoot "init-secrets.ps1")

    $resolvedJson = & docker compose --env-file $envFile -f docker-compose.yml config --format json
    if ($LASTEXITCODE -ne 0) { throw "Compose configuration validation failed." }
    $resolved = ($resolvedJson -join "`n") | ConvertFrom-Json
    $corpus = @($resolved.services."state-init".volumes) |
        Where-Object { $_.target -eq "/corpus" } |
        Select-Object -First 1
    $legacyDb = if ($corpus) { Join-Path $corpus.source "settings.db" } else { $null }

    if ($legacyDb -and (Test-Path -LiteralPath $legacyDb -PathType Leaf)) {
        $migrationScript = Join-Path $PSScriptRoot "migrate_v02.ps1"
        Invoke-SetupScript -Path $migrationScript
        if (-not $ApplyMigration) {
            throw "A legacy database requires migration. Review the preview above, then rerun setup.cmd -ApplyMigration."
        }
        Invoke-SetupScript -Path $migrationScript -Arguments @("-Apply")
    }

    $composeScript = Join-Path $PSScriptRoot "compose.ps1"
    Invoke-SetupScript -Path $composeScript -Arguments @("config", "--quiet")
    $upArgs = @("up", "--detach")
    if (-not $SkipBuild) { $upArgs += "--build" }
    Invoke-SetupScript -Path $composeScript -Arguments $upArgs

    Write-Host ""
    Write-Host "local-ai-suite is starting in the background."
    Write-Host "Admin UI: http://localhost:$($resolved.services.admin.ports[0].published)"
    Write-Host "MCP endpoint: http://localhost:$($resolved.services.gateway.ports[0].published)/mcp"
    Write-Host "Admin token: config/secrets/admin_token.txt"
}
finally {
    Pop-Location
}
