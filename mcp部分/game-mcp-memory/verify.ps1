$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required but was not found in PATH."
}

Write-Host "Running offline tests..."
uv run pytest

Write-Host "Checking local configuration without printing secrets..."
$Status = uv run python -c "from app.config import get_settings; s=get_settings(); print('configured' if s.api_key_configured else 'missing')"
if ($Status -ne "configured") {
    throw "MEMORY_API_KEY is missing in .env."
}

Write-Host "Offline verification passed. Run .\start.ps1 for the live provider test."
