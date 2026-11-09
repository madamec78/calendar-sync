# Spustí Calendar Sync nativně na Windows (bez Dockeru) proti SKUTEČNÉMU Google.
# Tajné soubory zůstávají ve složce .local\ (je v .gitignore):
#   .local\google_oauth_client.json  - OAuth klient typu "Desktop app" (nahrajete vy)
#   .local\admin_password.txt        - heslo správce (vyžádá se při prvním spuštění)
#   .local\session_secret.txt        - náhodný klíč pro cookies (vygeneruje se)
# Data (DB, token, zálohy) jsou v .local\data.
# Použití:  .\run-local.ps1 [-Port 8080]
param([int]$Port = 8080)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force .local | Out-Null

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install -q -e ".[dev]"
}
if (-not (Test-Path ".local\session_secret.txt")) {
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes) | Set-Content -NoNewline -Encoding ascii .local\session_secret.txt
}
if (-not (Test-Path ".local\admin_password.txt")) {
    $secure = Read-Host "Zvolte heslo správce (min. 12 znaků)" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if ($plain.Length -lt 12) { throw "Heslo je příliš krátké." }
    Set-Content -NoNewline -Encoding utf8 .local\admin_password.txt $plain
}
if (-not (Test-Path ".local\google_oauth_client.json")) {
    Write-Warning "Chybí .local\google_oauth_client.json - bez něj nelze provést Google autorizaci (viz README, kapitola Google příprava)."
}

$env:DATA_DIR = (Resolve-Path .local).Path + "\data"
$env:ADMIN_PASSWORD_FILE = (Resolve-Path .local\admin_password.txt).Path
$env:SESSION_SECRET_FILE = (Resolve-Path .local\session_secret.txt).Path
$env:GOOGLE_OAUTH_CLIENT_FILE = (Resolve-Path .local).Path + "\google_oauth_client.json"
$env:PUBLIC_BASE_URL = "http://localhost:$Port"
$env:COOKIE_SECURE = "false"   # čisté HTTP na localhostu; za HTTPS proxy ponechte true

Write-Host "Calendar Sync běží na http://localhost:$Port (Ctrl+C ukončí)"
& .\.venv\Scripts\python.exe -m calendar_sync.cli serve --host 127.0.0.1 --port $Port
