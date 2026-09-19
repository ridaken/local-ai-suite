param(
    [switch]$Legacy,
    [Alias("d")]
    [switch]$Detach,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArgs
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$EnvFile = Join-Path $repoRoot "config/.env"
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Configuration file not found. Copy config/.env.example to config/.env first."
}
if (-not $ComposeArgs) { $ComposeArgs = @("up", "--detach", "--build") }
if ($Detach) {
    if ($ComposeArgs[0] -ne "up") {
        throw "-Detach/-d can only be used with the Compose 'up' command."
    }
    if ($ComposeArgs -notcontains "--detach") { $ComposeArgs += "--detach" }
}
if ($ComposeArgs[0] -in @("up", "create", "run", "config")) {
    $secretDir = Join-Path $repoRoot "config/secrets"
    $secretNames = @("admin_token.txt", "mcp_api_key.txt", "kagi_api_key.txt", "ncbi_api_key.txt")
    if ($Legacy) { $secretNames += "mcpo_api_key.txt" }
    foreach ($name in $secretNames) {
        $path = Join-Path $secretDir $name
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Docker secret file is missing: $path`nRun scripts/init-secrets.ps1 before Compose."
        }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Docker secret path must be a file, but is a directory: $path`nRun scripts/init-secrets.ps1 to repair empty directory placeholders."
        }
    }
}
$composeOptions = @("compose", "--env-file", $EnvFile, "-f", "docker-compose.yml")
if ($Legacy) {
    $composeOptions += @("-f", "docker-compose.legacy-mcpo.yml", "--profile", "legacy-mcpo")
}

Push-Location $repoRoot
try {
    # Show only resolved storage and port bindings, never the complete environment/secrets.
    $resolvedJson = & docker @composeOptions config --format json
    if ($LASTEXITCODE -ne 0) { throw "Compose configuration validation failed." }
    $resolved = ($resolvedJson -join "`n") | ConvertFrom-Json

    if ($ComposeArgs[0] -in @("up", "start")) {
        $corpus = @($resolved.services."state-init".volumes) |
            Where-Object { $_.target -eq "/corpus" } |
            Select-Object -First 1
        if ($corpus -and (Test-Path -LiteralPath (Join-Path $corpus.source "settings.db") -PathType Leaf)) {
            throw "Legacy settings database found at $($corpus.source)\settings.db.`nRun scripts/migrate_v02.ps1 to preview the required v0.2 migration, then rerun it with -Apply."
        }
    }

    foreach ($name in @("admin", "gateway", "kiwix", "qdrant")) {
        $service = $resolved.services.$name
        foreach ($volume in $service.volumes) {
            Write-Host ("{0}: {1} -> {2}" -f $name, $volume.source, $volume.target)
        }
        foreach ($port in $service.ports) {
            Write-Host ("{0}: {1}:{2} -> {3}" -f $name, $port.host_ip, $port.published, $port.target)
        }
    }
    & docker @composeOptions @ComposeArgs
    $composeExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $composeExitCode
