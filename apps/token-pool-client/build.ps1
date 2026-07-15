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

$BuildVersion = Join-Path $Root "src\token_pool_client\_build_version.py"
Set-Content -LiteralPath $BuildVersion -Encoding utf8 -Value "VERSION = `"$Version`"`n"
try {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
    & $Python -m PyInstaller --noconfirm --clean token-pool-client.spec
} finally {
    Remove-Item -LiteralPath $BuildVersion -Force -ErrorAction SilentlyContinue
}

$ReleaseRoot = Join-Path $Root $OutputDirectory
$PackageRoot = Join-Path $ReleaseRoot "TokenPoolClient-win-x64"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $PackageRoot
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot "app") | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "dist\TokenPoolClient\*") (Join-Path $PackageRoot "app")
Copy-Item -LiteralPath (Join-Path $Root "installer\Install-TokenPoolClient.ps1") `
    -Destination (Join-Path $PackageRoot "Launch-TokenPoolClient.ps1")

if (-not $ConfigurationPath) {
    $ConfigurationPath = Join-Path $Root "client-config.json"
}
if (-not (Test-Path -LiteralPath $ConfigurationPath)) {
    throw "Client configuration was not found: $ConfigurationPath"
}
$Config = Get-Content -Raw -LiteralPath $ConfigurationPath | ConvertFrom-Json
Copy-Item -LiteralPath $ConfigurationPath -Destination (Join-Path $PackageRoot "client-config.json")
$Certificate = [string]$Config.ca_certificate
if (-not [IO.Path]::IsPathRooted($Certificate)) {
    $Certificate = Join-Path (Split-Path -Parent $ConfigurationPath) $Certificate
}
if (-not (Test-Path -LiteralPath $Certificate)) {
    throw "Pinned CA certificate was not found: $Certificate"
}
Copy-Item -LiteralPath $Certificate -Destination (Join-Path $PackageRoot "server.crt")
$Config.ca_certificate = "server.crt"
$Config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $PackageRoot "client-config.json") -Encoding utf8
@{ version = $Version; built_at = [DateTime]::UtcNow.ToString("o") } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PackageRoot "version.json") -Encoding utf8

New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
$Zip = Join-Path $ReleaseRoot "TokenPoolClient-win-x64.zip"
Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
$Compressed = $false
for ($Attempt = 1; $Attempt -le 8 -and -not $Compressed; $Attempt++) {
    try {
        Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
        Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $Zip -CompressionLevel Optimal -ErrorAction Stop
        $Compressed = $true
    } catch {
        if ($Attempt -eq 8) { throw }
        Write-Warning "Package files are temporarily busy (attempt $Attempt/8); retrying..."
        Start-Sleep -Seconds 2
    }
}
if (-not $Compressed -or -not (Test-Path -LiteralPath $Zip)) {
    throw "Token Pool Client ZIP was not created."
}
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Zip).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$Zip.sha256" -Encoding ascii -Value "$Hash  TokenPoolClient-win-x64.zip"
Write-Output "PACKAGE=$Zip"
Write-Output "SHA256=$Hash"
