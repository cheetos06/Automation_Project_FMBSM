param(
    [string]$Version = "1.0.0-dev",
    [string]$ConfigurationPath = "",
    [string]$OutputDirectory = "release"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Venv = Join-Path $Root ".build-venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    python -m venv $Venv
}
$Python = Join-Path $Venv "Scripts\python.exe"
& $Python -m pip install --upgrade pip wheel
& $Python -m pip install -r requirements-build.txt

$BuildVersion = Join-Path $Root "src\token_pool_admin\_build_version.py"
Set-Content -LiteralPath $BuildVersion -Encoding utf8 -Value "VERSION = `"$Version`"`n"
try {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
    & $Python -m PyInstaller --noconfirm --clean token-pool-admin.spec
} finally {
    Remove-Item -LiteralPath $BuildVersion -Force -ErrorAction SilentlyContinue
}

if (-not $ConfigurationPath) {
    $ConfigurationPath = Join-Path $Root "admin-config.json"
}
if (-not (Test-Path -LiteralPath $ConfigurationPath)) {
    throw "Private admin configuration was not found: $ConfigurationPath"
}
$Config = Get-Content -Raw -LiteralPath $ConfigurationPath | ConvertFrom-Json
$Certificate = [string]$Config.ca_certificate
if (-not [IO.Path]::IsPathRooted($Certificate)) {
    $Certificate = Join-Path (Split-Path -Parent $ConfigurationPath) $Certificate
}
if (-not (Test-Path -LiteralPath $Certificate)) {
    throw "Pinned server certificate was not found: $Certificate"
}

$ReleaseRoot = Join-Path $Root $OutputDirectory
$PackageRoot = Join-Path $ReleaseRoot "TokenPoolAdmin-win-x64"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $PackageRoot
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot "app") | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "dist\TokenPoolAdmin\*") (Join-Path $PackageRoot "app")
Copy-Item -LiteralPath (Join-Path $Root "..\token-pool-client\assets\token-pool-logo.png") -Destination (Join-Path $PackageRoot "app\token-pool-logo.png")
Copy-Item -LiteralPath (Join-Path $Root "..\token-pool-client\assets\token-pool-logo.ico") -Destination (Join-Path $PackageRoot "app\token-pool-logo.ico")
Copy-Item -LiteralPath $ConfigurationPath -Destination (Join-Path $PackageRoot "app\admin-config.json")
Copy-Item -LiteralPath $Certificate -Destination (Join-Path $PackageRoot "app\server.crt")
$PackagedConfig = Get-Content -Raw -LiteralPath (Join-Path $PackageRoot "app\admin-config.json") | ConvertFrom-Json
$PackagedConfig.ca_certificate = "server.crt"
$PackagedConfig | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $PackageRoot "app\admin-config.json") -Encoding utf8
@{
    version = $Version
    built_at = [DateTime]::UtcNow.ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PackageRoot "version.json") -Encoding utf8

$Zip = Join-Path $ReleaseRoot "TokenPoolAdmin-win-x64.zip"
Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $Zip -CompressionLevel Optimal
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Zip).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$Zip.sha256" -Encoding ascii -Value "$Hash  TokenPoolAdmin-win-x64.zip"
Write-Output "PACKAGE=$Zip"
Write-Output "SHA256=$Hash"
