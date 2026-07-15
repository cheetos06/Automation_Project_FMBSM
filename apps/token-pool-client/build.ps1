param(
    [string]$Version = "1.0.0-dev",
    [string]$ConfigurationPath = "",
    [string]$OutputDirectory = "release",
    [string]$ArtifactMirrorBaseUrl = "http://35.180.210.11/downloads/token-client"
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

function Get-TreeContentHash([string]$Path) {
    $Resolved = (Resolve-Path -LiteralPath $Path).Path
    $Description = New-Object Text.StringBuilder
    Get-ChildItem -LiteralPath $Resolved -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            $Relative = $_.FullName.Substring($Resolved.Length + 1).Replace("\", "/").ToLowerInvariant()
            if ($Relative -eq "base_library.zip") {
                Add-Type -AssemblyName System.IO.Compression.FileSystem
                $Archive = [IO.Compression.ZipFile]::OpenRead($_.FullName)
                $ArchiveDescription = New-Object Text.StringBuilder
                try {
                    foreach ($Entry in @($Archive.Entries | Sort-Object FullName)) {
                        $EntryStream = $Entry.Open()
                        $EntryHasher = [Security.Cryptography.SHA256]::Create()
                        try {
                            $EntryHash = ([BitConverter]::ToString($EntryHasher.ComputeHash($EntryStream))).Replace("-", "").ToLowerInvariant()
                        } finally {
                            $EntryHasher.Dispose()
                            $EntryStream.Dispose()
                        }
                        [void]$ArchiveDescription.Append($Entry.FullName).Append("`0").Append($Entry.Length).Append("`0").Append($EntryHash).Append("`n")
                    }
                } finally {
                    $Archive.Dispose()
                }
                $ArchiveHasher = [Security.Cryptography.SHA256]::Create()
                try {
                    $ArchiveBytes = [Text.Encoding]::UTF8.GetBytes($ArchiveDescription.ToString())
                    $FileHash = ([BitConverter]::ToString($ArchiveHasher.ComputeHash($ArchiveBytes))).Replace("-", "").ToLowerInvariant()
                } finally {
                    $ArchiveHasher.Dispose()
                }
            } else {
                $FileHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
            }
            [void]$Description.Append($Relative).Append("`0").Append($_.Length).Append("`0").Append($FileHash).Append("`n")
        }
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [Text.Encoding]::UTF8.GetBytes($Description.ToString())
        return ([BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $Hasher.Dispose()
    }
}

function Get-RuntimeId {
    $PythonDescription = (& $Python -c "import platform,sys; print('|'.join((platform.python_implementation(), platform.python_version(), platform.architecture()[0], sys.platform)))").Trim()
    $IdentityPackages = "altgraph,cffi,cryptography,greenlet,packaging,pefile,playwright,pycparser,pyee,pyinstaller,pyinstaller-hooks-contrib,pywin32-ctypes,setuptools,typing-extensions,wheel"
    $FrozenPackages = @(& $Python -c "from importlib.metadata import version; names='$IdentityPackages'.split(','); print(chr(10).join(n+'=='+version(n) for n in names))" | Sort-Object)
    $SpecHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Root "token-pool-client.spec")).Hash.ToLowerInvariant()
    $Description = @{
        schema = 1
        python = $PythonDescription
        packages = $FrozenPackages
        spec_sha256 = $SpecHash
    } | ConvertTo-Json -Depth 5 -Compress
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [Text.Encoding]::UTF8.GetBytes($Description)
        return ([BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $Hasher.Dispose()
    }
}

function Compress-WithRetry([string]$Source, [string]$Destination) {
    $Compressed = $false
    for ($Attempt = 1; $Attempt -le 8 -and -not $Compressed; $Attempt++) {
        try {
            Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
            Compress-Archive -Path $Source -DestinationPath $Destination -CompressionLevel Optimal -ErrorAction Stop
            $Compressed = $true
        } catch {
            if ($Attempt -eq 8) { throw }
            Write-Warning "Package files are temporarily busy (attempt $Attempt/8); retrying..."
            Start-Sleep -Seconds 2
        }
    }
    if (-not $Compressed -or -not (Test-Path -LiteralPath $Destination)) {
        throw "ZIP was not created: $Destination"
    }
}

function Split-ReleaseFile([string]$Path, [int]$ChunkSize) {
    Get-ChildItem -LiteralPath (Split-Path -Parent $Path) -Filter "$(Split-Path -Leaf $Path).part*" -ErrorAction SilentlyContinue |
        Remove-Item -Force
    $InputStream = [IO.File]::OpenRead($Path)
    $Parts = @()
    try {
        $Buffer = New-Object byte[] $ChunkSize
        while (($Read = $InputStream.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
            $PartPath = "${Path}.part{0:D3}" -f $Parts.Count
            $OutputStream = [IO.File]::Create($PartPath)
            try { $OutputStream.Write($Buffer, 0, $Read) } finally { $OutputStream.Dispose() }
            $Parts += Get-Item -LiteralPath $PartPath
        }
    } finally {
        $InputStream.Dispose()
    }
    return $Parts
}

$BuildVersion = Join-Path $Root "src\token_pool_client\_build_version.py"
Set-Content -LiteralPath $BuildVersion -Encoding utf8 -Value "VERSION = `"$Version`"`n"
try {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist
    & $Python -m PyInstaller --noconfirm --clean token-pool-client.spec
} finally {
    Remove-Item -LiteralPath $BuildVersion -Force -ErrorAction SilentlyContinue
}

$RuntimeInternal = Join-Path $Root "dist\TokenPoolClient\_internal"
if (-not (Test-Path -LiteralPath $RuntimeInternal)) {
    throw "PyInstaller runtime directory was not created."
}
$RuntimeId = Get-RuntimeId
$RuntimeContentHash = Get-TreeContentHash $RuntimeInternal

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
@{
    version = $Version
    built_at = [DateTime]::UtcNow.ToString("o")
    runtime_id = $RuntimeId
    runtime_content_sha256 = $RuntimeContentHash
} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PackageRoot "version.json") -Encoding utf8

New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
$Zip = Join-Path $ReleaseRoot "TokenPoolClient-win-x64.zip"
Compress-WithRetry (Join-Path $PackageRoot "*") $Zip
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Zip).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$Zip.sha256" -Encoding ascii -Value "$Hash  TokenPoolClient-win-x64.zip"

# Corporate proxies can terminate long GitHub asset streams after only a few
# megabytes. Publish small ordered parts as well as the full ZIP; the installer
# rejoins them and verifies this same full-file checksum before activation.
$FullParts = @(Split-ReleaseFile $Zip 4MB)
$PartCount = $FullParts.Count
if ($PartCount -lt 2) {
    throw "Expected a multi-part client package, created only $PartCount part(s)."
}

# The app layer changes frequently, while the Python/Playwright runtime is stable.
# Publishing it separately lets an installed client reuse its verified runtime and
# download only the small executable/configuration layer on ordinary updates.
$AppLayerRoot = Join-Path $ReleaseRoot "TokenPoolClient-app-win-x64"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $AppLayerRoot
New-Item -ItemType Directory -Force -Path (Join-Path $AppLayerRoot "app") | Out-Null
Copy-Item -LiteralPath (Join-Path $PackageRoot "app\TokenPoolClient.exe") -Destination (Join-Path $AppLayerRoot "app\TokenPoolClient.exe")
foreach ($Name in "Launch-TokenPoolClient.ps1", "client-config.json", "server.crt", "version.json") {
    Copy-Item -LiteralPath (Join-Path $PackageRoot $Name) -Destination (Join-Path $AppLayerRoot $Name)
}
$AppZip = Join-Path $ReleaseRoot "TokenPoolClient-app-win-x64.zip"
Compress-WithRetry (Join-Path $AppLayerRoot "*") $AppZip
$AppHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $AppZip).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$AppZip.sha256" -Encoding ascii -Value "$AppHash  TokenPoolClient-app-win-x64.zip"
Get-ChildItem -LiteralPath $ReleaseRoot -Filter "TokenPoolClient-app-win-x64.zip.part*" -ErrorAction SilentlyContinue |
    Remove-Item -Force

$Tag = "token-client-v$Version"
$Manifest = [ordered]@{
    schema_version = 1
    version = $Version
    tag = $Tag
    runtime_id = $RuntimeId
    runtime_content_sha256 = $RuntimeContentHash
    mirror_base_url = "$($ArtifactMirrorBaseUrl.TrimEnd('/'))/$Tag"
    app = [ordered]@{
        name = "TokenPoolClient-app-win-x64.zip"
        sha256 = $AppHash
        size = (Get-Item -LiteralPath $AppZip).Length
    }
    full = [ordered]@{
        name = "TokenPoolClient-win-x64.zip"
        sha256 = $Hash
        size = (Get-Item -LiteralPath $Zip).Length
        parts = @(
            Get-ChildItem -LiteralPath $ReleaseRoot -Filter "TokenPoolClient-win-x64.zip.part*" |
                Sort-Object Name |
                ForEach-Object { [ordered]@{ name = $_.Name; size = $_.Length } }
        )
    }
}
$ManifestPath = Join-Path $ReleaseRoot "TokenPoolClient-release.json"
$Manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ManifestPath -Encoding utf8

Write-Output "PACKAGE=$Zip"
Write-Output "SHA256=$Hash"
Write-Output "PARTS=$PartCount"
Write-Output "APP_PACKAGE=$AppZip"
Write-Output "APP_SHA256=$AppHash"
Write-Output "RUNTIME_ID=$RuntimeId"
Write-Output "RUNTIME_CONTENT_SHA256=$RuntimeContentHash"
