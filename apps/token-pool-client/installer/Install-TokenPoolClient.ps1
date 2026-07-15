param(
    [switch]$InstallOnly,
    [switch]$Background,
    [int]$WaitForProcessId = 0
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repository = if ($env:TOKEN_POOL_GITHUB_REPOSITORY) { $env:TOKEN_POOL_GITHUB_REPOSITORY } else { "cheetos06/Automation_Project_FMBSM" }
$InstallRoot = if ($env:TOKEN_POOL_INSTALL_ROOT) { $env:TOKEN_POOL_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "FMBSM\TokenPoolClient" }
$VersionsRoot = Join-Path $InstallRoot "versions"
$RuntimesRoot = Join-Path $InstallRoot "runtimes"
$CurrentFile = Join-Path $InstallRoot "current.json"
$LogFile = Join-Path $InstallRoot "launcher.log"
$AllowedMirrorHost = "35.180.210.11"
$ParallelDownloads = 8
$DirectInstallerRun = [string]::IsNullOrWhiteSpace([string]$MyInvocation.MyCommand.Path)
New-Item -ItemType Directory -Force -Path $VersionsRoot, $RuntimesRoot | Out-Null

if ($WaitForProcessId -gt 0) {
    Wait-Process -Id $WaitForProcessId -ErrorAction SilentlyContinue
}

function Write-LauncherLog([string]$Message) {
    "{0:o} {1}" -f [DateTime]::UtcNow, $Message | Add-Content -LiteralPath $LogFile -Encoding utf8
    Write-Host "[FMBSM] $Message"
}

function Move-DirectoryWithRetry([string]$Source, [string]$Destination) {
    for ($Attempt = 1; $Attempt -le 20; $Attempt++) {
        try {
            [IO.Directory]::Move($Source, $Destination)
            return
        } catch {
            if ($Attempt -eq 20) { throw }
            Start-Sleep -Milliseconds (100 * $Attempt)
        }
    }
}

function Get-AppRelease {
    if ($env:TOKEN_POOL_RELEASE_METADATA) {
        $Releases = @(Get-Content -Raw -LiteralPath $env:TOKEN_POOL_RELEASE_METADATA | ConvertFrom-Json)
    } else {
    $Headers = @{ "User-Agent" = "FMBSM-Token-Pool-Installer"; "Accept" = "application/vnd.github+json" }
    if ($env:GITHUB_TOKEN) { $Headers.Authorization = "Bearer $($env:GITHUB_TOKEN)" }
    $Releases = Invoke-RestMethod -UseBasicParsing -Headers $Headers -Uri "https://api.github.com/repos/$Repository/releases?per_page=30"
    }
    return $Releases |
        Where-Object { -not $_.draft -and $_.tag_name -like "token-client-v*" } |
        Sort-Object {[DateTime]$_.published_at} -Descending |
        Select-Object -First 1
}

function Get-ReleaseAsset($Release, [string]$Name) {
    return $Release.assets | Where-Object name -eq $Name | Select-Object -First 1
}

function Receive-Url(
    [string]$Uri,
    [string]$Destination,
    [hashtable]$Headers,
    [string]$DisplayName,
    [int]$TimeoutSeconds = 120
) {
    if (-not $Uri) { throw "Asset $DisplayName has no download URL." }
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        try {
            Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $Destination
            Invoke-WebRequest -UseBasicParsing -Headers $Headers -Uri $Uri -OutFile $Destination -TimeoutSec $TimeoutSeconds
            if (-not (Test-Path -LiteralPath $Destination) -or (Get-Item -LiteralPath $Destination).Length -le 0) {
                throw "Downloaded asset is empty."
            }
            return
        } catch {
            if ($Attempt -eq 3) { throw }
            Write-LauncherLog "Download retry $Attempt/3 for ${DisplayName}: $($_.Exception.Message)"
            Start-Sleep -Seconds (2 * $Attempt)
        }
    }
}

function Receive-ReleaseAsset($Asset, [string]$Destination) {
    $Headers = @{ "User-Agent" = "FMBSM-Token-Pool-Installer"; "Accept" = "application/octet-stream" }
    if ($env:GITHUB_TOKEN) {
        $Headers.Authorization = "Bearer $($env:GITHUB_TOKEN)"
        $Uri = [string]$Asset.url
    } else {
        $Uri = [string]$Asset.browser_download_url
    }
    Receive-Url $Uri $Destination $Headers ([string]$Asset.name)
}

function Get-MirrorUri($Manifest, [string]$Name) {
    if (-not $Manifest -or -not $Manifest.mirror_base_url) { return $null }
    if ($Name -notmatch '^[A-Za-z0-9._-]+$') { return $null }
    try {
        $Base = [Uri]([string]$Manifest.mirror_base_url)
        if ($Base.Scheme -notin @("http", "https") -or $Base.Host -ne $AllowedMirrorHost) { return $null }
        return "$([string]$Manifest.mirror_base_url.TrimEnd('/'))/$Name"
    } catch {
        return $null
    }
}

function Receive-MirroredReleaseAsset($Release, $Manifest, [string]$Name, [string]$Destination) {
    $MirrorUri = Get-MirrorUri $Manifest $Name
    if ($MirrorUri) {
        try {
            Write-LauncherLog "Downloading $Name from AWS mirror"
            Receive-Url $MirrorUri $Destination @{ "User-Agent" = "FMBSM-Token-Pool-Installer" } $Name 30
            return
        } catch {
            Write-LauncherLog "AWS mirror failed for ${Name}; falling back to GitHub: $($_.Exception.Message)"
        }
    }
    $Asset = Get-ReleaseAsset $Release $Name
    if (-not $Asset) { throw "Release $($Release.tag_name) is missing $Name." }
    Write-LauncherLog "Downloading $Name from GitHub"
    Receive-ReleaseAsset $Asset $Destination
}

function Get-ReleaseManifest($Release) {
    $Asset = Get-ReleaseAsset $Release "TokenPoolClient-release.json"
    if (-not $Asset) { return $null }
    $Path = Join-Path $env:TEMP "TokenPoolClient-release-$PID.json"
    try {
        Receive-ReleaseAsset $Asset $Path
        $Manifest = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    } finally {
        Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $Path
    }
    if (
        $Manifest.schema_version -ne 1 -or
        [string]$Manifest.tag -ne [string]$Release.tag_name -or
        [string]$Manifest.runtime_id -notmatch '^[a-f0-9]{64}$' -or
        [string]$Manifest.runtime_content_sha256 -notmatch '^[a-f0-9]{64}$' -or
        [string]$Manifest.app.sha256 -notmatch '^[a-f0-9]{64}$' -or
        [string]$Manifest.full.sha256 -notmatch '^[a-f0-9]{64}$'
    ) {
        throw "Release $($Release.tag_name) has an invalid client manifest."
    }
    $EffectiveRuntimeId = if ([string]$Manifest.canonical_runtime_id) {
        [string]$Manifest.canonical_runtime_id
    } else { [string]$Manifest.runtime_id }
    $EffectiveContentHash = if ([string]$Manifest.canonical_runtime_content_sha256) {
        [string]$Manifest.canonical_runtime_content_sha256
    } else { [string]$Manifest.runtime_content_sha256 }
    $CompatibleRuntimeIds = @($Manifest.compatible_runtime_ids)
    if ($CompatibleRuntimeIds.Count -eq 0) { $CompatibleRuntimeIds = @($EffectiveRuntimeId) }
    if (
        $EffectiveRuntimeId -notmatch '^[a-f0-9]{64}$' -or
        $EffectiveContentHash -notmatch '^[a-f0-9]{64}$' -or
        @($CompatibleRuntimeIds | Where-Object { [string]$_ -notmatch '^[a-f0-9]{64}$' }).Count -gt 0 -or
        $CompatibleRuntimeIds -notcontains $EffectiveRuntimeId
    ) {
        throw "Release $($Release.tag_name) has invalid runtime compatibility metadata."
    }
    $Manifest | Add-Member -NotePropertyName effective_runtime_id -NotePropertyValue $EffectiveRuntimeId
    $Manifest | Add-Member -NotePropertyName effective_runtime_content_sha256 -NotePropertyValue $EffectiveContentHash
    $Manifest | Add-Member -NotePropertyName effective_compatible_runtime_ids -NotePropertyValue $CompatibleRuntimeIds
    return $Manifest
}

if (-not ("FmbsmTimeoutWebClient" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Net;
public sealed class FmbsmTimeoutWebClient : WebClient {
    public int TimeoutMilliseconds { get; set; }
    public FmbsmTimeoutWebClient() { TimeoutMilliseconds = 180000; }
    protected override WebRequest GetWebRequest(Uri address) {
        WebRequest request = base.GetWebRequest(address);
        request.Timeout = TimeoutMilliseconds;
        HttpWebRequest http = request as HttpWebRequest;
        if (http != null) http.ReadWriteTimeout = TimeoutMilliseconds;
        return request;
    }
}
"@
}

function Invoke-ParallelPartAttempt($Downloads, [string]$SourceName) {
    $Failed = New-Object Collections.ArrayList
    for ($Offset = 0; $Offset -lt $Downloads.Count; $Offset += $ParallelDownloads) {
        $Last = [Math]::Min($Offset + $ParallelDownloads - 1, $Downloads.Count - 1)
        $Batch = @($Downloads[$Offset..$Last])
        $Active = @()
        foreach ($Download in $Batch) {
            Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $Download.destination
            $Client = New-Object FmbsmTimeoutWebClient
            $Client.Headers.Add("User-Agent", "FMBSM-Token-Pool-Installer")
            if ($Download.authorization) { $Client.Headers.Add("Authorization", $Download.authorization) }
            if ($Download.accept) { $Client.Headers.Add("Accept", $Download.accept) }
            $Task = $Client.DownloadFileTaskAsync([Uri]$Download.uri, [string]$Download.destination)
            $Active += [pscustomobject]@{ download = $Download; client = $Client; task = $Task }
        }
        foreach ($Entry in $Active) {
            try {
                [void]$Entry.task.GetAwaiter().GetResult()
                $File = Get-Item -LiteralPath $Entry.download.destination -ErrorAction Stop
                if ($File.Length -le 0 -or ($Entry.download.size -gt 0 -and $File.Length -ne $Entry.download.size)) {
                    throw "Downloaded part has an unexpected size."
                }
                Write-LauncherLog "Downloaded $($Entry.download.name) from $SourceName"
            } catch {
                Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $Entry.download.destination
                [void]$Failed.Add($Entry.download)
                Write-LauncherLog "$SourceName failed for $($Entry.download.name): $($_.Exception.Message)"
            } finally {
                $Entry.client.Dispose()
            }
        }
    }
    return @($Failed)
}

function Receive-ReleaseParts($Release, $Manifest, $PartSpecs, [string]$PartDirectory) {
    New-Item -ItemType Directory -Force -Path $PartDirectory | Out-Null
    $Downloads = @()
    foreach ($Spec in $PartSpecs) {
        $Name = [string]$Spec.name
        if ($Name -notmatch '^TokenPoolClient-win-x64\.zip\.part[0-9]{3}$') {
            throw "Release contains an invalid part name: $Name"
        }
        $Asset = Get-ReleaseAsset $Release $Name
        if (-not $Asset) { throw "Release $($Release.tag_name) is missing $Name." }
        $Downloads += [pscustomobject]@{
            name = $Name
            size = [long]$Spec.size
            destination = Join-Path $PartDirectory $Name
            asset = $Asset
        }
    }

    $Pending = $Downloads
    $MirrorAvailable = $Manifest -and (Get-MirrorUri $Manifest $Downloads[0].name)
    if ($MirrorAvailable) {
        $MirrorDownloads = @($Pending | ForEach-Object {
            [pscustomobject]@{
                name = $_.name; size = $_.size; destination = $_.destination
                uri = Get-MirrorUri $Manifest $_.name
                authorization = $null; accept = $null; asset = $_.asset
            }
        })
        Write-LauncherLog "Downloading $($MirrorDownloads.Count) part(s) from AWS with $ParallelDownloads concurrent connections"
        $Pending = Invoke-ParallelPartAttempt $MirrorDownloads "AWS"
    }

    for ($Attempt = 1; $Pending.Count -gt 0 -and $Attempt -le 2; $Attempt++) {
        $GitHubDownloads = @($Pending | ForEach-Object {
            $Asset = $_.asset
            $Uri = if ($env:GITHUB_TOKEN) { [string]$Asset.url } else { [string]$Asset.browser_download_url }
            [pscustomobject]@{
                name = $_.name; size = $_.size; destination = $_.destination
                uri = $Uri
                authorization = if ($env:GITHUB_TOKEN) { "Bearer $($env:GITHUB_TOKEN)" } else { $null }
                accept = "application/octet-stream"; asset = $Asset
            }
        })
        Write-LauncherLog "Downloading $($GitHubDownloads.Count) remaining part(s) from GitHub (attempt $Attempt/2)"
        $Pending = Invoke-ParallelPartAttempt $GitHubDownloads "GitHub"
    }
    if ($Pending.Count -gt 0) {
        throw "Unable to download $($Pending.Count) release part(s)."
    }
    return $Downloads
}

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

function Get-RuntimeInternal([string]$RuntimeId) {
    return Join-Path (Join-Path $RuntimesRoot $RuntimeId) "_internal"
}

function Test-RuntimeStructure([string]$Internal) {
    return (
        (Get-ChildItem -LiteralPath $Internal -Filter "python*.dll" -File -ErrorAction SilentlyContinue | Select-Object -First 1) -and
        (Test-Path -LiteralPath (Join-Path $Internal "playwright\driver\node.exe"))
    )
}

function Test-RuntimeCache([string]$RuntimeId, [string]$ContentHash) {
    $RuntimeRoot = Join-Path $RuntimesRoot $RuntimeId
    $Internal = Join-Path $RuntimeRoot "_internal"
    $Marker = Join-Path $RuntimeRoot "runtime.json"
    if (-not (Test-Path -LiteralPath $Internal) -or -not (Test-Path -LiteralPath $Marker)) { return $false }
    try {
        $Metadata = Get-Content -Raw -LiteralPath $Marker | ConvertFrom-Json
        return [string]$Metadata.runtime_id -eq $RuntimeId -and (Test-RuntimeStructure $Internal)
    } catch {
        return $false
    }
}

function Initialize-RuntimeCache(
    [string]$SourceInternal,
    [string]$RuntimeId,
    [string]$ContentHash,
    [bool]$MoveSource
) {
    if (Test-RuntimeCache $RuntimeId $ContentHash) { return Get-RuntimeInternal $RuntimeId }
    $RuntimeRoot = Join-Path $RuntimesRoot $RuntimeId
    $Staging = "$RuntimeRoot.staging-$PID"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath $Staging
    New-Item -ItemType Directory -Force -Path $Staging | Out-Null
    $StagingInternal = Join-Path $Staging "_internal"
    if ($MoveSource) {
        Move-DirectoryWithRetry $SourceInternal $StagingInternal
    } else {
        New-Item -ItemType Directory -Force -Path $StagingInternal | Out-Null
        Copy-Item -Recurse -Force -Path (Join-Path $SourceInternal "*") -Destination $StagingInternal
    }
    if (-not (Test-RuntimeStructure $StagingInternal)) {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath $Staging
        throw "Runtime cache is incomplete."
    }
    @{
        runtime_id = $RuntimeId
        runtime_content_sha256 = $ContentHash
        cached_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Staging "runtime.json") -Encoding utf8
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath $RuntimeRoot
    Move-DirectoryWithRetry $Staging $RuntimeRoot
    Write-LauncherLog "Cached runtime $RuntimeId"
    return Get-RuntimeInternal $RuntimeId
}

function Connect-Runtime([string]$AppDirectory, [string]$RuntimeInternal) {
    $Link = Join-Path $AppDirectory "_internal"
    if (Test-Path -LiteralPath $Link) {
        Remove-Item -Recurse -Force -LiteralPath $Link
    }
    try {
        New-Item -ItemType Junction -Path $Link -Target $RuntimeInternal -ErrorAction Stop | Out-Null
        Write-LauncherLog "Attached cached runtime with a directory junction"
    } catch {
        Write-LauncherLog "Junction unavailable; copying cached runtime locally: $($_.Exception.Message)"
        New-Item -ItemType Directory -Force -Path $Link | Out-Null
        Copy-Item -Recurse -Force -Path (Join-Path $RuntimeInternal "*") -Destination $Link
    }
}

function Find-ReusableRuntime($Current, [string]$RuntimeId, [string]$ContentHash, $CompatibleRuntimeIds) {
    if (Test-RuntimeCache $RuntimeId $ContentHash) { return Get-RuntimeInternal $RuntimeId }
    if (-not $Current -or -not $Current.path) { return $null }
    $Source = Join-Path ([string]$Current.path) "app\_internal"
    if (-not (Test-Path -LiteralPath $Source)) { return $null }

    $CurrentRuntimeId = [string]$Current.runtime_id
    $Matches = $CurrentRuntimeId -and @($CompatibleRuntimeIds) -contains $CurrentRuntimeId
    if (-not $Matches) {
        Write-LauncherLog "Checking installed runtime for one-time fast-update migration"
        $Matches = (Get-TreeContentHash $Source) -eq $ContentHash
    }
    if (-not $Matches) { return $null }
    return Initialize-RuntimeCache $Source $RuntimeId $ContentHash $false
}

function Test-FileChecksum([string]$Path, [string]$Expected, [string]$Description) {
    $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    if ($Expected.ToLowerInvariant() -ne $Actual) { throw "$Description checksum mismatch." }
}

function Receive-ChunkedPackage($Release, $Manifest, $PartSpecs, [string]$Destination, [string]$Label) {
    $PartDirectory = Join-Path $env:TEMP "TokenPoolClient-$Label-parts-$($Release.tag_name)-$PID"
    try {
        $Downloads = Receive-ReleaseParts $Release $Manifest $PartSpecs $PartDirectory
        $Combined = [IO.File]::Create($Destination)
        try {
            foreach ($Download in $Downloads) {
                $PartStream = [IO.File]::OpenRead($Download.destination)
                try { $PartStream.CopyTo($Combined) } finally { $PartStream.Dispose() }
            }
        } finally {
            $Combined.Dispose()
        }
    } finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath $PartDirectory
    }
}

function Expand-ClientPackage([string]$Archive, [string]$Destination) {
    $Tar = Get-Command "tar.exe" -ErrorAction SilentlyContinue
    if ($Tar) {
        & $Tar.Source -xf $Archive -C $Destination
        if ($LASTEXITCODE -eq 0) { return }
        Write-LauncherLog "Native ZIP extraction failed with exit code $LASTEXITCODE; using PowerShell fallback"
    }
    Expand-Archive -LiteralPath $Archive -DestinationPath $Destination -Force
}

function Receive-FullPackage($Release, $Manifest, [string]$Zip) {
    if ($Manifest) {
        $PartSpecs = @($Manifest.full.parts)
        $Expected = [string]$Manifest.full.sha256
    } else {
        $PartSpecs = @($Release.assets | Where-Object name -like "TokenPoolClient-win-x64.zip.part*" | Sort-Object name | ForEach-Object {
            [pscustomobject]@{ name = $_.name; size = $_.size }
        })
        $ChecksumAsset = Get-ReleaseAsset $Release "TokenPoolClient-win-x64.zip.sha256"
        if (-not $ChecksumAsset) { throw "Release $($Release.tag_name) is missing its checksum." }
        $Checksum = "$Zip.sha256"
        Receive-ReleaseAsset $ChecksumAsset $Checksum
        $Expected = ((Get-Content -Raw -LiteralPath $Checksum).Trim() -split '\s+')[0].ToLowerInvariant()
        Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $Checksum
    }
    if ($PartSpecs.Count -gt 0) {
        Receive-ChunkedPackage $Release $Manifest $PartSpecs $Zip "full"
    } else {
        Receive-MirroredReleaseAsset $Release $Manifest "TokenPoolClient-win-x64.zip" $Zip
    }
    Test-FileChecksum $Zip $Expected "Downloaded full app"
}

function Receive-AppPackage($Release, $Manifest, [string]$Destination) {
    Receive-MirroredReleaseAsset $Release $Manifest ([string]$Manifest.app.name) $Destination
    Test-FileChecksum $Destination ([string]$Manifest.app.sha256) "Downloaded app layer"
}

function Install-Release($Release, $Current) {
    if (-not $Release) { throw "No Token Pool Client release is available." }
    $Started = [Diagnostics.Stopwatch]::StartNew()
    $Tag = [string]$Release.tag_name
    $Manifest = Get-ReleaseManifest $Release
    $Destination = Join-Path $VersionsRoot $Tag

    if (
        -not (Test-Path -LiteralPath (Join-Path $Destination "app\TokenPoolClient.exe")) -or
        -not (Test-Path -LiteralPath (Join-Path $Destination "app\_internal"))
    ) {
        $Staging = "$Destination.staging-$PID"
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath $Staging
        New-Item -ItemType Directory -Force -Path $Staging | Out-Null
        try {
            $ReusableRuntime = $null
            if ($Manifest) {
                $ReusableRuntime = Find-ReusableRuntime `
                    $Current `
                    ([string]$Manifest.effective_runtime_id) `
                    ([string]$Manifest.effective_runtime_content_sha256) `
                    @($Manifest.effective_compatible_runtime_ids)
            }
            if ($ReusableRuntime -and (Get-ReleaseAsset $Release ([string]$Manifest.app.name))) {
                $AppZip = Join-Path $env:TEMP "TokenPoolClient-app-$Tag-$PID.zip"
                Write-LauncherLog "Fast update selected for $Tag; reusing runtime $($Manifest.effective_runtime_id)"
                Receive-AppPackage $Release $Manifest $AppZip
                Expand-ClientPackage $AppZip $Staging
                Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $AppZip
                Connect-Runtime (Join-Path $Staging "app") $ReusableRuntime
                $RuntimeId = [string]$Manifest.effective_runtime_id
                $RuntimeContentHash = [string]$Manifest.effective_runtime_content_sha256
            } else {
                $Zip = Join-Path $env:TEMP "TokenPoolClient-$Tag-$PID.zip"
                Write-LauncherLog "Full install selected for $Tag"
                Receive-FullPackage $Release $Manifest $Zip
                Expand-ClientPackage $Zip $Staging
                Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath $Zip
                $SourceInternal = Join-Path $Staging "app\_internal"
                if (-not (Test-Path -LiteralPath $SourceInternal)) { throw "Downloaded release has no runtime." }
                if ($Manifest) {
                    $RuntimeId = [string]$Manifest.effective_runtime_id
                    $RuntimeContentHash = [string]$Manifest.effective_runtime_content_sha256
                } else {
                    $RuntimeContentHash = Get-TreeContentHash $SourceInternal
                    $RuntimeId = "legacy-$RuntimeContentHash"
                }
                $RuntimeInternal = Initialize-RuntimeCache $SourceInternal $RuntimeId $RuntimeContentHash $true
                Connect-Runtime (Join-Path $Staging "app") $RuntimeInternal
            }
            if (-not (Test-Path -LiteralPath (Join-Path $Staging "app\TokenPoolClient.exe"))) {
                throw "Downloaded release has no TokenPoolClient.exe."
            }
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath $Destination
            Move-DirectoryWithRetry $Staging $Destination
        } catch {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath $Staging
            throw
        }
    } else {
        $VersionMetadata = Get-Content -Raw -LiteralPath (Join-Path $Destination "version.json") | ConvertFrom-Json
        $RuntimeId = [string]$VersionMetadata.runtime_id
        $RuntimeContentHash = [string]$VersionMetadata.runtime_content_sha256
    }

    Copy-Item -Force -LiteralPath (Join-Path $Destination "Launch-TokenPoolClient.ps1") -Destination (Join-Path $InstallRoot "Launch-TokenPoolClient.ps1")
    Copy-Item -Force -LiteralPath (Join-Path $Destination "client-config.json") -Destination (Join-Path $InstallRoot "client-config.json")
    Copy-Item -Force -LiteralPath (Join-Path $Destination "server.crt") -Destination (Join-Path $InstallRoot "server.crt")
    $Logo = Join-Path $Destination "app\token-pool-logo.ico"
    if (Test-Path -LiteralPath $Logo) {
        Copy-Item -Force -LiteralPath $Logo -Destination (Join-Path $InstallRoot "token-pool-logo.ico")
    }
    $Config = Get-Content -Raw -LiteralPath (Join-Path $InstallRoot "client-config.json") | ConvertFrom-Json
    $Config.ca_certificate = "server.crt"
    $Config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $InstallRoot "client-config.json") -Encoding utf8
    @{
        tag = $Tag
        path = $Destination
        runtime_id = $RuntimeId
        runtime_content_sha256 = $RuntimeContentHash
        updated_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath "$CurrentFile.tmp" -Encoding utf8
    Move-Item -Force -LiteralPath "$CurrentFile.tmp" -Destination $CurrentFile
    $Started.Stop()
    Write-LauncherLog "Activated $Tag in $([Math]::Round($Started.Elapsed.TotalSeconds, 1)) second(s)"
    return $Destination
}

function Install-Shortcut {
    if ($env:TOKEN_POOL_SKIP_SHORTCUT -eq "1") { return }
    $Launcher = Join-Path $InstallRoot "Launch-TokenPoolClient.ps1"
    if (-not (Test-Path -LiteralPath $Launcher)) { throw "Installed launcher is missing." }
    $Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\FMBSM"
    New-Item -ItemType Directory -Force -Path $Programs | Out-Null
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut((Join-Path $Programs "Token Pool Client.lnk"))
    $Shortcut.TargetPath = "powershell.exe"
    $Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`""
    $Shortcut.WorkingDirectory = $InstallRoot
    $Icon = Join-Path $InstallRoot "token-pool-logo.ico"
    $Shortcut.IconLocation = if (Test-Path -LiteralPath $Icon) { $Icon } else { "shell32.dll,14" }
    $Shortcut.Save()
}

try {
    $Release = Get-AppRelease
    $Current = if (Test-Path $CurrentFile) { Get-Content -Raw $CurrentFile | ConvertFrom-Json } else { $null }
    $CurrentExecutable = if ($Current -and $Current.path) { Join-Path ([string]$Current.path) "app\TokenPoolClient.exe" } else { "" }
    $CurrentRuntime = if ($Current -and $Current.path) { Join-Path ([string]$Current.path) "app\_internal" } else { "" }
    $ReleaseChanged = (
        -not $Current -or
        $Current.tag -ne $Release.tag_name -or
        -not (Test-Path -LiteralPath $CurrentExecutable) -or
        -not (Test-Path -LiteralPath $CurrentRuntime)
    )
    $AppPath = if ($ReleaseChanged) {
        Install-Release $Release $Current
    } else {
        [string]$Current.path
    }
    $VersionLogo = Join-Path $AppPath "app\token-pool-logo.ico"
    $InstalledLogo = Join-Path $InstallRoot "token-pool-logo.ico"
    $LogoChanged = $false
    if (Test-Path -LiteralPath $VersionLogo) {
        $LogoChanged = -not (Test-Path -LiteralPath $InstalledLogo) -or
            (Get-FileHash -Algorithm SHA256 -LiteralPath $VersionLogo).Hash -ne
            (Get-FileHash -Algorithm SHA256 -LiteralPath $InstalledLogo).Hash
        if ($LogoChanged) {
            Copy-Item -Force -LiteralPath $VersionLogo -Destination $InstalledLogo
        }
    }
    $ShortcutPath = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\FMBSM\Token Pool Client.lnk"
    if ($InstallOnly -or $ReleaseChanged -or $LogoChanged -or -not (Test-Path $ShortcutPath)) {
        Install-Shortcut
    }
    if ($DirectInstallerRun) {
        Write-LauncherLog "Installed under $InstallRoot"
        Write-LauncherLog "Start Menu shortcut: $ShortcutPath"
    }
    if (-not $InstallOnly) {
        $Executable = Join-Path $AppPath "app\TokenPoolClient.exe"
        if ($Background) {
            Start-Process -FilePath $Executable -ArgumentList "--background" -WorkingDirectory (Join-Path $AppPath "app")
        } else {
            Start-Process -FilePath $Executable -WorkingDirectory (Join-Path $AppPath "app")
        }
    }
} catch {
    Write-LauncherLog "Update failed: $($_.Exception.Message)"
    if (Test-Path $CurrentFile) {
        $Fallback = Get-Content -Raw $CurrentFile | ConvertFrom-Json
        $Executable = Join-Path ([string]$Fallback.path) "app\TokenPoolClient.exe"
        if (Test-Path $Executable) {
            if (-not $InstallOnly) {
                if ($Background) {
                    Start-Process -FilePath $Executable -ArgumentList "--background" -WorkingDirectory (Split-Path $Executable)
                } else {
                    Start-Process -FilePath $Executable -WorkingDirectory (Split-Path $Executable)
                }
            }
            exit 0
        }
    }
    if ($InstallOnly) { throw }
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("Token Pool Client could not start: $($_.Exception.Message)", "FMBSM") | Out-Null
    exit 1
}
