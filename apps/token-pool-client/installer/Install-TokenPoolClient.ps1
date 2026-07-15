param([switch]$InstallOnly)

$ErrorActionPreference = "Stop"
$Repository = "cheetos06/Automation_Project_FMBSM"
$InstallRoot = Join-Path $env:LOCALAPPDATA "FMBSM\TokenPoolClient"
$VersionsRoot = Join-Path $InstallRoot "versions"
$CurrentFile = Join-Path $InstallRoot "current.json"
$LogFile = Join-Path $InstallRoot "launcher.log"
New-Item -ItemType Directory -Force -Path $VersionsRoot | Out-Null

function Write-LauncherLog([string]$Message) {
    "{0:o} {1}" -f [DateTime]::UtcNow, $Message | Add-Content -LiteralPath $LogFile -Encoding utf8
}

function Get-AppRelease {
    $Headers = @{ "User-Agent" = "FMBSM-Token-Pool-Installer"; "Accept" = "application/vnd.github+json" }
    if ($env:GITHUB_TOKEN) { $Headers.Authorization = "Bearer $($env:GITHUB_TOKEN)" }
    $Releases = Invoke-RestMethod -Headers $Headers -Uri "https://api.github.com/repos/$Repository/releases?per_page=30"
    return $Releases |
        Where-Object { -not $_.draft -and $_.tag_name -like "token-client-v*" } |
        Sort-Object {[DateTime]$_.published_at} -Descending |
        Select-Object -First 1
}

function Receive-ReleaseAsset($Asset, [string]$Destination) {
    $Headers = @{ "User-Agent" = "FMBSM-Token-Pool-Installer"; "Accept" = "application/octet-stream" }
    if ($env:GITHUB_TOKEN) {
        $Headers.Authorization = "Bearer $($env:GITHUB_TOKEN)"
        $Uri = [string]$Asset.url
    } else {
        $Uri = [string]$Asset.browser_download_url
    }
    if (-not $Uri) { throw "Release asset $($Asset.name) has no download URL." }
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        try {
            Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $Destination
            Invoke-WebRequest -Headers $Headers -Uri $Uri -OutFile $Destination -TimeoutSec 120
            if (-not (Test-Path -LiteralPath $Destination) -or (Get-Item -LiteralPath $Destination).Length -le 0) {
                throw "Downloaded asset is empty."
            }
            return
        } catch {
            if ($Attempt -eq 3) { throw }
            Write-LauncherLog "Download retry $Attempt/3 for $($Asset.name): $($_.Exception.Message)"
            Start-Sleep -Seconds (2 * $Attempt)
        }
    }
}

function Install-Release($Release) {
    if (-not $Release) { throw "No Token Pool Client release is available." }
    $Tag = [string]$Release.tag_name
    $Asset = $Release.assets | Where-Object name -eq "TokenPoolClient-win-x64.zip" | Select-Object -First 1
    $ChecksumAsset = $Release.assets | Where-Object name -eq "TokenPoolClient-win-x64.zip.sha256" | Select-Object -First 1
    $Parts = @($Release.assets | Where-Object name -like "TokenPoolClient-win-x64.zip.part*" | Sort-Object name)
    if ((-not $Asset -and $Parts.Count -eq 0) -or -not $ChecksumAsset) {
        throw "Release $Tag is missing its Windows asset or checksum."
    }
    $Destination = Join-Path $VersionsRoot $Tag
    if (-not (Test-Path (Join-Path $Destination "app\TokenPoolClient.exe"))) {
        $Zip = Join-Path $env:TEMP "TokenPoolClient-$Tag.zip"
        $Checksum = Join-Path $env:TEMP "TokenPoolClient-$Tag.sha256"
        if ($Parts.Count -gt 0) {
            Write-LauncherLog "Downloading $Tag in $($Parts.Count) proxy-safe part(s)"
            $Combined = [IO.File]::Create($Zip)
            try {
                for ($Index = 0; $Index -lt $Parts.Count; $Index++) {
                    $Part = $Parts[$Index]
                    $PartFile = Join-Path $env:TEMP "$($Part.name).$PID"
                    Write-LauncherLog "Downloading part $($Index + 1)/$($Parts.Count)"
                    Receive-ReleaseAsset $Part $PartFile
                    $PartStream = [IO.File]::OpenRead($PartFile)
                    try {
                        $PartStream.CopyTo($Combined)
                    } finally {
                        $PartStream.Dispose()
                        Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $PartFile
                    }
                }
            } finally {
                $Combined.Dispose()
            }
        } else {
            Receive-ReleaseAsset $Asset $Zip
        }
        Receive-ReleaseAsset $ChecksumAsset $Checksum
        $Expected = ((Get-Content -Raw -LiteralPath $Checksum).Trim() -split '\s+')[0].ToLowerInvariant()
        $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Zip).Hash.ToLowerInvariant()
        if ($Expected -ne $Actual) { throw "Downloaded app checksum mismatch." }
        $Staging = "$Destination.staging-$PID"
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Staging
        New-Item -ItemType Directory -Path $Staging | Out-Null
        Expand-Archive -LiteralPath $Zip -DestinationPath $Staging -Force
        if (-not (Test-Path (Join-Path $Staging "app\TokenPoolClient.exe"))) {
            throw "Downloaded release has no TokenPoolClient.exe."
        }
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Destination
        Move-Item -LiteralPath $Staging -Destination $Destination
    }
    Copy-Item -Force -LiteralPath (Join-Path $Destination "Launch-TokenPoolClient.ps1") -Destination (Join-Path $InstallRoot "Launch-TokenPoolClient.ps1")
    Copy-Item -Force -LiteralPath (Join-Path $Destination "client-config.json") -Destination (Join-Path $InstallRoot "client-config.json")
    Copy-Item -Force -LiteralPath (Join-Path $Destination "server.crt") -Destination (Join-Path $InstallRoot "server.crt")
    $Config = Get-Content -Raw -LiteralPath (Join-Path $InstallRoot "client-config.json") | ConvertFrom-Json
    $Config.ca_certificate = "server.crt"
    $Config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $InstallRoot "client-config.json") -Encoding utf8
    @{ tag = $Tag; path = $Destination; updated_at = [DateTime]::UtcNow.ToString("o") } |
        ConvertTo-Json | Set-Content -LiteralPath "$CurrentFile.tmp" -Encoding utf8
    Move-Item -Force -LiteralPath "$CurrentFile.tmp" -Destination $CurrentFile
    Write-LauncherLog "Activated $Tag"
    return $Destination
}

function Install-Shortcut {
    $Launcher = Join-Path $InstallRoot "Launch-TokenPoolClient.ps1"
    if (-not (Test-Path -LiteralPath $Launcher)) { throw "Installed launcher is missing." }
    $Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\FMBSM"
    New-Item -ItemType Directory -Force -Path $Programs | Out-Null
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut((Join-Path $Programs "Token Pool Client.lnk"))
    $Shortcut.TargetPath = "powershell.exe"
    $Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`""
    $Shortcut.WorkingDirectory = $InstallRoot
    $Shortcut.IconLocation = "shell32.dll,14"
    $Shortcut.Save()
}

try {
    $Release = Get-AppRelease
    $Current = if (Test-Path $CurrentFile) { Get-Content -Raw $CurrentFile | ConvertFrom-Json } else { $null }
    $AppPath = if (-not $Current -or $Current.tag -ne $Release.tag_name) {
        Install-Release $Release
    } else {
        [string]$Current.path
    }
    $ShortcutPath = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\FMBSM\Token Pool Client.lnk"
    if ($InstallOnly -or -not (Test-Path $ShortcutPath)) {
        Install-Shortcut
    }
    if (-not $InstallOnly) {
        Start-Process -FilePath (Join-Path $AppPath "app\TokenPoolClient.exe") -WorkingDirectory (Join-Path $AppPath "app")
    }
} catch {
    Write-LauncherLog "Update failed: $($_.Exception.Message)"
    if (Test-Path $CurrentFile) {
        $Fallback = Get-Content -Raw $CurrentFile | ConvertFrom-Json
        $Executable = Join-Path ([string]$Fallback.path) "app\TokenPoolClient.exe"
        if (Test-Path $Executable) {
            Start-Process -FilePath $Executable -WorkingDirectory (Split-Path $Executable)
            exit 0
        }
    }
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("Token Pool Client could not start: $($_.Exception.Message)", "FMBSM") | Out-Null
    exit 1
}
