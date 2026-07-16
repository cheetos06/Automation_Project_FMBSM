param(
    [string]$Endpoint = "http://35.180.210.11",
    [string]$CertificatePath = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Root "admin-config.json"
$TargetCertificate = Join-Path $Root "server.crt"

if ((Test-Path -LiteralPath $ConfigPath) -and -not $Force) {
    Write-Output "Private administrator configuration already exists; it was not changed."
    exit 0
}
if (-not $CertificatePath) {
    $CertificatePath = Join-Path $Root "..\token-pool-client\server.crt"
}
if (-not (Test-Path -LiteralPath $CertificatePath)) {
    throw "Pinned token server certificate was not found: $CertificatePath"
}
if (-not (
    $Endpoint.StartsWith("http://", [StringComparison]::OrdinalIgnoreCase) -or
    $Endpoint.StartsWith("https://", [StringComparison]::OrdinalIgnoreCase)
)) {
    throw "The administrator API endpoint must use HTTP or HTTPS."
}

$KeyBytes = New-Object byte[] 32
$Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $Generator.GetBytes($KeyBytes)
} finally {
    $Generator.Dispose()
}
$AdminKey = ([BitConverter]::ToString($KeyBytes)).Replace("-", "").ToLowerInvariant()
@{
    endpoint = $Endpoint.TrimEnd("/")
    admin_key = $AdminKey
    ca_certificate = "server.crt"
} | ConvertTo-Json | Set-Content -LiteralPath $ConfigPath -Encoding utf8
Copy-Item -LiteralPath $CertificatePath -Destination $TargetCertificate -Force
Write-Output "Private administrator configuration created at $ConfigPath."
Write-Output "The credential was not printed and is excluded from Git."
