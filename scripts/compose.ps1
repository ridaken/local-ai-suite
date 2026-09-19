param(
    [switch]$Legacy,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArgs
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$EnvFile = Join-Path $repoRoot "config/.env"
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Configuration file not found. Copy config/.env.example to config/.env first."
}
if (-not $ComposeArgs) { $ComposeArgs = @("up", "-d", "--build") }
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
