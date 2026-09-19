param([switch]$Force)

$ErrorActionPreference = "Stop"
$secretDir = Join-Path $PSScriptRoot "..\config\secrets"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force -Path $secretDir | Out-Null

function Initialize-SecretPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (Test-Path -LiteralPath $Path -PathType Leaf) { return }

    $children = @(Get-ChildItem -Force -LiteralPath $Path)
    if ($children.Count -ne 0) {
        throw "Secret path is a non-empty directory and will not be replaced: $Path"
    }
    Remove-Item -LiteralPath $Path
    Write-Host "Replaced empty directory at secret path $Path"
}

foreach ($name in @("admin_token.txt", "mcp_api_key.txt", "mcpo_api_key.txt")) {
    $path = Join-Path $secretDir $name
    Initialize-SecretPath -Path $path
    if ((Test-Path -LiteralPath $path -PathType Leaf) -and -not $Force) {
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
    Initialize-SecretPath -Path $path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        [IO.File]::WriteAllText($path, "", $utf8NoBom)
        Write-Host "Created optional empty $path"
    }
}
