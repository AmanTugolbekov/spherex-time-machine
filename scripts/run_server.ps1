param(
    [Parameter(Mandatory = $true)]
    [string]$EncryptedKey,
    [int]$Port = 8008
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

$secureKey = ConvertTo-SecureString $EncryptedKey
$plainKey = [System.Net.NetworkCredential]::new("", $secureKey).Password

try {
    $env:GEMINI_API_KEY = $plainKey
    $env:GEMINI_MODEL = "gemini-3.6-flash"
    Set-Location -LiteralPath $projectRoot
    & $python -m uvicorn server.app:app --host 127.0.0.1 --port $Port
}
finally {
    $env:GEMINI_API_KEY = $null
    $plainKey = $null
    $secureKey.Dispose()
}
