<#
.SYNOPSIS
  Download a ZIM file into ZIM_DIR (explicitly — nothing is auto-fetched).

.DESCRIPTION
  Reads ZIM_DIR from config/.env and downloads the given ZIM URL into it.
  Browse available ZIMs at https://download.kiwix.org/zim/ (e.g. wikipedia/,
  devdocs/, stack_exchange/). For a small first test, a Wikipedia "mini" or
  "nopic" build is a good choice.

.EXAMPLE
  ./scripts/download_zim.ps1 -Url "https://download.kiwix.org/zim/wikipedia/wikipedia_en_100_nopic_2024-06.zim"

.NOTES
  For very large files, a download manager (aria2c) is far faster and resumable:
    aria2c -x8 -d "<ZIM_DIR>" "<url>"
#>
param(
    [Parameter(Mandatory = $true)][string]$Url
)

$ErrorActionPreference = "Stop"
$envFile = Join-Path $PSScriptRoot "..\config\.env"
if (-not (Test-Path $envFile)) {
    throw "config/.env not found. Copy config/.env.example to config/.env and set ZIM_DIR."
}

$zimDir = (Get-Content $envFile |
    Where-Object { $_ -match '^\s*ZIM_DIR\s*=' } |
    Select-Object -First 1) -replace '^\s*ZIM_DIR\s*=\s*', ''
$zimDir = $zimDir.Trim().Trim('"')
if ([string]::IsNullOrWhiteSpace($zimDir)) { throw "ZIM_DIR is not set in config/.env." }

if (-not (Test-Path $zimDir)) {
    New-Item -ItemType Directory -Force -Path $zimDir | Out-Null
}

$fileName = Split-Path $Url -Leaf
$dest = Join-Path $zimDir $fileName
Write-Host "Downloading $fileName" -ForegroundColor Cyan
Write-Host "  into $zimDir" -ForegroundColor DarkGray
Invoke-WebRequest -Uri $Url -OutFile $dest
Write-Host "Done: $dest" -ForegroundColor Green
Write-Host "Restart kiwix to pick it up:  docker compose --env-file config/.env restart kiwix" -ForegroundColor Yellow
