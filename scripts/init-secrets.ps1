param([switch]$Force)

$ErrorActionPreference = "Stop"
$secretDir = Join-Path $PSScriptRoot "..\config\secrets"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
foreach ($name in @("admin_token.txt", "mcp_api_key.txt", "mcpo_api_key.txt")) {
    $path = Join-Path $secretDir $name
    if ((Test-Path -LiteralPath $path) -and -not $Force) {
        Write-Host "Keeping existing $path"
        continue
    }
    $bytes = New-Object byte[] 48
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    $value = [Convert]::ToBase64String($bytes)
    [IO.File]::WriteAllText($path, $value, $utf8NoBom)
    Write-Host "Created $path"
}

foreach ($name in @("kagi_api_key.txt", "ncbi_api_key.txt")) {
    $path = Join-Path $secretDir $name
    if (-not (Test-Path -LiteralPath $path)) {
        [IO.File]::WriteAllText($path, "", $utf8NoBom)
        Write-Host "Created optional empty $path"
    }
}
