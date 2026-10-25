<#
  proxmox-lab installer for Windows 10/11.  Run it in PowerShell (no admin needed):

      irm https://raw.githubusercontent.com/madmaxvoltro/proxmox-lab/main/install.ps1 | iex

  It downloads lab.exe for your CPU (x64 or ARM64), verifies its checksum, puts it in
  %LOCALAPPDATA%\Programs\lab, adds that folder to your PATH, then starts `lab connect`.

  Optional, set before running:
      $env:LAB_HOST       the Proxmox host's Tailscale name (skips the question)
      $env:LAB_VERSION    a release tag such as v0.1.0 (default: the latest)
      $env:LAB_NO_CONNECT = '1'   install only
      $env:LAB_NO_PATH = '1'      do not touch PATH (tests)
      $env:LAB_REPO       owner/name (default madmaxvoltro/proxmox-lab)
      $env:LAB_API_URL / $env:LAB_DOWNLOAD_URL   for mirrors and tests
  To remove it:   & "$env:LOCALAPPDATA\Programs\lab\lab.exe" uninstall
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the download is many times faster without the progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$repo = if ($env:LAB_REPO) { $env:LAB_REPO } else { 'madmaxvoltro/proxmox-lab' }
$apiUrl = if ($env:LAB_API_URL) { $env:LAB_API_URL } else { "https://api.github.com/repos/$repo" }
$downloadUrl = if ($env:LAB_DOWNLOAD_URL) { $env:LAB_DOWNLOAD_URL } else { "https://github.com/$repo/releases/download" }
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\lab'

# Look: a title box, then a tree of steps. Glyphs are built from code points so this file stays pure ASCII,
# and fall back to ASCII in consoles that cannot draw them (legacy conhost).
$fancy = [bool]($env:WT_SESSION -or $env:TERM_PROGRAM -or $env:ConEmuPID)
if ($fancy) { try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { $fancy = $false } }
if ($fancy) {
    $g = @{ bar = [char]0x2502; bul = [char]0x25C6; tick = [char]0x2714; cross = [char]0x2718; end = [char]0x2514
            tl = [char]0x256D; tr = [char]0x256E; bl = [char]0x2570; br = [char]0x256F; hz = [char]0x2500 }
} else {
    $g = @{ bar = '|'; bul = '*'; tick = '+'; cross = 'x'; end = '`'; tl = '+'; tr = '+'; bl = '+'; br = '+'; hz = '-' }
}
function Banner($subtitle) {
    $w = [Math]::Max(15, $subtitle.Length) + 4
    $rule = [string]$g.hz * $w
    $side = if ($fancy) { [string][char]0x2502 } else { '|' }
    Write-Host ""
    Write-Host "  $($g.tl)$rule$($g.tr)" -ForegroundColor DarkGray
    Write-Host "  $side  " -NoNewline -ForegroundColor DarkGray; Write-Host ('proxmox-lab'.PadRight($w - 2)) -NoNewline -ForegroundColor Cyan; Write-Host $side -ForegroundColor DarkGray
    Write-Host "  $side  " -NoNewline -ForegroundColor DarkGray; Write-Host ($subtitle.PadRight($w - 2)) -NoNewline -ForegroundColor Gray; Write-Host $side -ForegroundColor DarkGray
    Write-Host "  $($g.bl)$rule$($g.br)" -ForegroundColor DarkGray
}
function Bar() { Write-Host "  $($g.bar)  " -NoNewline -ForegroundColor DarkGray }
function Step($text) { Write-Host "  $($g.bar)" -ForegroundColor DarkGray; Write-Host "  $($g.bul) " -NoNewline -ForegroundColor Cyan; Write-Host $text -ForegroundColor White }
function Ok($text) { Bar; Write-Host "$($g.tick) " -NoNewline -ForegroundColor Green; Write-Host $text }
function Note($text) { Bar; Write-Host $text -ForegroundColor DarkGray }
function Warn($text) { Bar; Write-Host "! " -NoNewline -ForegroundColor Yellow; Write-Host $text -ForegroundColor Yellow }
function Done($text) { Write-Host "  $($g.bar)" -ForegroundColor DarkGray; Write-Host "  $($g.end) " -NoNewline -ForegroundColor DarkGray; Write-Host $text -ForegroundColor White }
function Fail($text) { Write-Host ""; Write-Host "  $($g.cross) $text" -ForegroundColor Red; throw $text }

Banner 'disposable project VMs on Proxmox, safe for AI'

Step 'Checking this PC'
$osArch = try { [string][System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture } catch { $env:PROCESSOR_ARCHITECTURE }
$arch = if ($osArch -match 'Arm64|ARM64') { 'arm64' } else { 'x64' }
Ok "Windows $([Environment]::OSVersion.Version) ($arch)"
$ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
if ($ssh) { Ok "OpenSSH client found" }
else { Warn "The OpenSSH client is missing. Install it: Settings > System > Optional features > OpenSSH Client." }
$tailscale = Get-Command tailscale.exe -ErrorAction SilentlyContinue
if (-not $tailscale) { Warn "Tailscale was not found on this PC. Install it from https://tailscale.com/download and sign in." }
else { Ok "Tailscale found" }

Step 'Finding the release'
$version = $env:LAB_VERSION
if (-not $version) {
    try { $version = (Invoke-RestMethod -Uri "$apiUrl/releases/latest" -Headers @{ 'User-Agent' = 'proxmox-lab-installer' }).tag_name }
    catch { Fail "Could not read the latest release of $repo ($($_.Exception.Message)). Is there a release yet?" }
}
Ok $version

Step 'Downloading'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("lab-install-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $asset = "lab-windows-$arch.exe"
    $base = "$downloadUrl/$version"
    Invoke-WebRequest -Uri "$base/$asset" -OutFile (Join-Path $tmp $asset) -UseBasicParsing -Headers @{ 'User-Agent' = 'proxmox-lab-installer' }
    Invoke-WebRequest -Uri "$base/SHA256SUMS" -OutFile (Join-Path $tmp 'SHA256SUMS') -UseBasicParsing -Headers @{ 'User-Agent' = 'proxmox-lab-installer' }
    $line = Get-Content (Join-Path $tmp 'SHA256SUMS') | Where-Object { $_ -match "\s\*?$([regex]::Escape($asset))$" } | Select-Object -First 1
    if (-not $line) { Fail "No checksum for $asset in SHA256SUMS; not installing." }
    $expected = ($line -split '\s+')[0].ToLower()
    $actual = (Get-FileHash (Join-Path $tmp $asset) -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { Fail "Checksum mismatch (expected $expected, got $actual); not installing." }
    Ok "checksum verified"

    Step 'Installing'
    # a running copy (the approval helper) would block replacing the file
    Get-Process -Name lab -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$installDir*" } | Stop-Process -Force
    New-Item -ItemType Directory -Force -Path $installDir | Out-Null
    Copy-Item (Join-Path $tmp $asset) (Join-Path $installDir 'lab.exe') -Force
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $env:LAB_NO_PATH -and ($userPath -split ';') -notcontains $installDir) {
        [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $installDir).TrimStart(';'), 'User')
        Ok "added $installDir to your PATH (new terminals see it)"
    }
    $env:Path = $env:Path + ';' + $installDir
    Ok "installed $version to $installDir\lab.exe"
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

& (Join-Path $installDir 'lab.exe') version | ForEach-Object { Ok $_ }
if ($env:LAB_NO_CONNECT) {
    Done "Installed. Next, run:  lab connect"
    return
}
Done "Installed. Starting the connect wizard..."
$env:LAB_BANNER_SHOWN = '1'   # the wizard skips its own title box
$connectArgs = @()
if ($env:LAB_HOST) { $connectArgs += $env:LAB_HOST }
& (Join-Path $installDir 'lab.exe') connect @connectArgs
