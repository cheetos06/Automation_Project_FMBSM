param(
    [string]$HostName = "35.180.210.11",
    [string]$UserName = "ubuntu",
    [string]$KeyPath = "C:\Users\Anas.nmili\Desktop\AWS\LightsailDefaultKey-eu-west-3.pem"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $KeyPath)) {
    throw "SSH key not found: $KeyPath"
}
Write-Host "Following mail worker + token API logs. Press Ctrl+C to stop." -ForegroundColor Cyan
ssh -i $KeyPath -o ServerAliveInterval=20 -o ServerAliveCountMax=3 `
    "$UserName@$HostName" `
    "sudo journalctl -u fmbsm-email-bot -u fmbsm-token-api -u fmbsm-token-api-http -f -o short-iso"
