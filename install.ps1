# KAMP-K2 one-shot PowerShell installer.
#
# For non-technical users -- downloads the repo, checks Python + paramiko,
# prompts for printer IP, detects existing install, runs installer/revert.
# No manual SSH needed.
#
# Run from PowerShell (not cmd.exe). Recommended one-liner (uses bootstrap.ps1
# because iex cannot parse scripts that declare [CmdletBinding()] + param()):
#
#   iwr -useb https://raw.githubusercontent.com/grant0013/KAMP-K2/main/bootstrap.ps1 | iex
#
# Or download this file and run locally:
#   .\install.ps1
#
# Optional parameters (note: -PrinterHost, NOT -Host -- PowerShell reserves
# $Host as the built-in host object, so -Host is not allowed as a param name):
#   .\install.ps1 -PrinterHost 192.168.1.42
#   .\install.ps1 -PrinterHost 192.168.1.42 -Password mypass
#   .\install.ps1 -Revert                         # revert without menu
#   .\install.ps1 -PrinterHost 192.168.1.42 -Revert

[CmdletBinding()]
param(
    [string]$PrinterHost = "",
    [string]$Password = "creality_2024",
    [ValidateSet("auto", "F008", "F021")]
    [string]$Board = "auto",
    [switch]$Revert,
    [switch]$DryRun
)

# NOTE: we intentionally do NOT set `$ErrorActionPreference = "Stop"` globally.
# Windows PowerShell 5.1 raises NativeCommandError for ANY stderr output from
# a native exe when that preference is Stop -- which breaks perfectly normal
# Python probe calls (e.g. `python -c "import paramiko"` before paramiko is
# installed, which correctly writes a traceback to stderr). We use
# -ErrorAction Stop per-cmdlet where we genuinely want hard failure.
$InstallDir   = Join-Path $env:USERPROFILE "KAMP-K2"
$BackupDir    = Join-Path $env:USERPROFILE "KAMP-K2\backups"
$RepoZipUrl   = "https://github.com/grant0013/KAMP-K2/archive/refs/heads/main.zip"

function Write-Step($msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[x] $msg" -ForegroundColor Red }

function Test-Python {
    foreach ($cmd in @("python", "py")) {
        try {
            # 2>$null: some Python launchers write version to stderr; we don't
            # want PowerShell's native-stderr handling getting involved.
            $out = & $cmd --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -match "Python\s+3\.") {
                return $cmd
            }
        } catch { continue }
    }
    return $null
}

function Test-Winget {
    try {
        $null = & winget --version 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Install-PythonViaWinget {
    Write-Step "Installing Python via winget (Python.Python.3.12)..."
    # --scope user avoids UAC elevation; --silent skips the Python GUI;
    # --accept-* skips the prompts winget would otherwise throw.
    & winget install --exact --id Python.Python.3.12 `
        --scope user --silent `
        --accept-package-agreements --accept-source-agreements 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Err "winget Python install failed (exit $LASTEXITCODE)."
        return $false
    }
    # winget installs don't update the current session's PATH. Re-read
    # PATH from the registry (user + machine) so `python` resolves now.
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $env:Path = $machinePath + ';' + $userPath
    Write-Ok "Python installed via winget; PATH refreshed in this session."
    return $true
}

function Ensure-Python {
    $py = Test-Python
    if ($py) {
        Write-Ok "Python found: $py ($(& $py --version 2>&1))"
        return $py
    }
    Write-Warn "Python 3 not found on PATH."
    Write-Host ""

    if (Test-Winget) {
        Write-Host "winget is available on this machine. I can install" -ForegroundColor Yellow
        Write-Host "Python 3.12 for you (user-scoped, no admin needed)." -ForegroundColor Yellow
        Write-Host ""
        $yn = Read-Host "Install Python 3.12 via winget now? [Y/n]"
        if ($yn -ne "n") {
            if (Install-PythonViaWinget) {
                $py = Test-Python
                if ($py) {
                    Write-Ok "Python ready: $py ($(& $py --version 2>&1))"
                    return $py
                }
                Write-Err "Python installed but not on PATH. Close and"
                Write-Err "reopen PowerShell, then re-run this script."
                exit 1
            }
            # winget path failed; fall through to manual instructions.
        }
    } else {
        Write-Host "(winget not found on this machine -- usually Win 10" -ForegroundColor Gray
        Write-Host " pre-1809 or App Installer not present.)"             -ForegroundColor Gray
        Write-Host ""
    }

    Write-Host "Manual install: https://www.python.org/downloads/"                  -ForegroundColor Yellow
    Write-Host "IMPORTANT: tick 'Add Python to PATH' on the first screen."          -ForegroundColor Yellow
    Write-Host ""
    $open = Read-Host "Open the Python download page now? [Y/n]"
    if ($open -ne "n") {
        Start-Process "https://www.python.org/downloads/"
    }
    exit 1
}

function Ensure-Paramiko($py) {
    Write-Step "Checking paramiko..."
    # 2>$null: paramiko-not-installed writes a traceback to stderr; we expect
    # that on first run and only care about $LASTEXITCODE. Without 2>$null,
    # Windows PowerShell 5.1 with ErrorActionPreference=Stop would raise a
    # NativeCommandError on the stderr output and kill the script.
    $check = & $py -c "import paramiko; print(paramiko.__version__)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "paramiko present (version $check)"
        return
    }
    Write-Step "Installing paramiko (pip install --user)..."
    & $py -m pip install --user --quiet paramiko 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Err "pip install paramiko failed. Try manually:"
        Write-Err "  $py -m pip install --user paramiko"
        Write-Host ""
        Write-Host "If you are on a very new Python release (3.14+) and pip" -ForegroundColor Yellow
        Write-Host "complains about wheels, also try:" -ForegroundColor Yellow
        Write-Host "  $py -m pip install --user --upgrade pip setuptools wheel" -ForegroundColor Yellow
        Write-Host "  $py -m pip install --user paramiko" -ForegroundColor Yellow
        exit 1
    }
    # Verify install actually worked (pip sometimes exits 0 on a no-op)
    $verify = & $py -c "import paramiko; print(paramiko.__version__)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Err "paramiko installed but cannot be imported -- Python env "
        Write-Err "issue. Try running in a fresh PowerShell session."
        exit 1
    }
    Write-Ok "paramiko installed (version $verify)"
}

function Download-Repo {
    Write-Step "Downloading KAMP-K2 from GitHub..."
    $tmpZip = Join-Path $env:TEMP "KAMP-K2-main.zip"
    Invoke-WebRequest -Uri $RepoZipUrl -OutFile $tmpZip -UseBasicParsing -ErrorAction Stop

    if (Test-Path $InstallDir) {
        Write-Step "Removing previous install at $InstallDir..."
        # Preserve the backups directory across repo re-downloads.
        $preservedBackups = $null
        if (Test-Path $BackupDir) {
            $preservedBackups = Join-Path $env:TEMP "KAMP-K2-backups-preserve"
            if (Test-Path $preservedBackups) {
                Remove-Item -Recurse -Force $preservedBackups -ErrorAction Stop
            }
            Move-Item $BackupDir $preservedBackups -ErrorAction Stop
        }
        Remove-Item -Recurse -Force $InstallDir -ErrorAction Stop
        if ($preservedBackups) {
            New-Item -ItemType Directory -Path $BackupDir -Force -ErrorAction Stop | Out-Null
            Move-Item (Join-Path $preservedBackups "*") $BackupDir -ErrorAction Stop
            Remove-Item -Recurse -Force $preservedBackups -ErrorAction Stop
        }
    }
    Write-Step "Extracting to $InstallDir..."
    $tmpExtract = Join-Path $env:TEMP "KAMP-K2-extract"
    if (Test-Path $tmpExtract) { Remove-Item -Recurse -Force $tmpExtract -ErrorAction Stop }
    Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -ErrorAction Stop
    $inner = Get-ChildItem -Path $tmpExtract | Where-Object { $_.PSIsContainer } | Select-Object -First 1
    Move-Item $inner.FullName $InstallDir -ErrorAction Stop
    Remove-Item -Recurse -Force $tmpExtract, $tmpZip -ErrorAction SilentlyContinue
    Write-Ok "Repo ready at $InstallDir"
}

function Get-PrinterHost {
    if ($PrinterHost) { return $PrinterHost }
    Write-Host ""
    Write-Host "Find your printer's IP on the touchscreen:" -ForegroundColor Yellow
    Write-Host "  Settings -> Network -> IP Address (e.g. 192.168.1.170)" -ForegroundColor Yellow
    Write-Host ""
    do {
        $ip = Read-Host "Enter your printer's IP address"
        $ip = $ip.Trim()
    } while (-not ($ip -match "^\d{1,3}(\.\d{1,3}){3}$"))
    return $ip
}

function Run-Installer($py, [string[]]$extraArgs) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    $args = @("install_k2.py",
              "--host", $ip,
              "--password", $Password,
              "--board", $Board,
              "--local-backup-dir", $BackupDir)
    $args += $extraArgs
    if ($DryRun) { $args += "--dry-run" }

    Push-Location $InstallDir
    try {
        & $py @args
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

function Detect-Install($py) {
    Write-Step "Checking printer state at $ip..."
    $detectArgs = @("install_k2.py",
                    "--host", $ip,
                    "--password", $Password,
                    "--detect")
    Push-Location $InstallDir
    try {
        $out = & $py @detectArgs 2>&1 | Out-String
    } finally {
        Pop-Location
    }
    $status = ($out -split "`n" | Where-Object { $_ -match "KAMPK2_STATUS=" } | Select-Object -First 1)
    $board  = ($out -split "`n" | Where-Object { $_ -match "KAMPK2_BOARD=" }  | Select-Object -First 1)
    if ($status -match "KAMPK2_STATUS=(\w+)") { $s = $Matches[1] } else { $s = "unknown" }
    if ($board  -match "KAMPK2_BOARD=(\w+)")  { $b = $Matches[1] } else { $b = "unknown" }
    return @{ Status = $s; Board = $b; RawOutput = $out }
}

function Show-Menu($detected) {
    Write-Host ""
    Write-Host "================================================" -ForegroundColor Cyan
    Write-Host " KAMP-K2 is already installed on this printer." -ForegroundColor Cyan
    Write-Host " Board detected: $($detected.Board)" -ForegroundColor Cyan
    Write-Host "================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  [1] Update / reinstall (pulls latest from GitHub)"
    Write-Host "  [2] Revert (restore original Creality configs, remove KAMP-K2)"
    Write-Host "  [3] Exit without changes"
    Write-Host ""
    do {
        $choice = Read-Host "Choose [1-3]"
    } while ($choice -notin @("1", "2", "3"))
    return $choice
}

# --- main -------------------------------------------------------------------

Write-Host ""
Write-Host "================================" -ForegroundColor Cyan
Write-Host " KAMP-K2 PowerShell installer"   -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

# Some users hit a "silent exit" around Download-Repo where the script
# appeared to end with no error shown. Wrap everything in a try so any
# unhandled exception surfaces to stdout rather than vanishing.
try {
    $py = Ensure-Python
    Ensure-Paramiko $py
    # TLS 1.2 fallback: older Windows/PS 5.1 configs still default to
    # TLS 1.0 which GitHub has dropped. This is a no-op if already 1.2+.
    try {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor
            [Net.SecurityProtocolType]::Tls12
    } catch { }
    # Suppress the noisy IWR progress bar in PS 5.1 — it flickers the
    # console and has been known to mask error output.
    $ProgressPreference = 'SilentlyContinue'
    Download-Repo
} catch {
    Write-Host ""
    Write-Host "[x] Setup / download failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Stack:" -ForegroundColor Yellow
    Write-Host $_.ScriptStackTrace -ForegroundColor Yellow
    exit 1
}

$ip = Get-PrinterHost

# Short-circuit: explicit -Revert flag skips the menu.
if ($Revert) {
    Write-Step "Running revert against $ip..."
    $rc = Run-Installer $py @("--revert")
    exit $rc
}

# Detect existing install and branch.
$detected = Detect-Install $py
if ($detected.Status -eq "installed") {
    $choice = Show-Menu $detected
    switch ($choice) {
        "1" {
            Write-Step "Running update/reinstall against $ip..."
            $rc = Run-Installer $py @()
        }
        "2" {
            Write-Step "Running revert against $ip..."
            $rc = Run-Installer $py @("--revert")
        }
        "3" {
            Write-Ok "Exited without changes."
            exit 0
        }
    }
} elseif ($detected.Status -eq "fresh") {
    Write-Ok "No existing install detected. Proceeding with fresh install."
    Write-Step "Running installer against $ip (board=$($detected.Board))..."
    $rc = Run-Installer $py @()
} else {
    Write-Warn "Could not determine install state. Detect output:"
    Write-Host $detected.RawOutput
    $go = Read-Host "Proceed with install anyway? [y/N]"
    if ($go -ne "y") { exit 1 }
    $rc = Run-Installer $py @()
}

Write-Host ""
if ($rc -eq 0) {
    Write-Ok "Done!"
    Write-Host ""
    Write-Host "Local backups kept at: $BackupDir" -ForegroundColor Gray
    Write-Host "These survive printer firmware updates. Keep them safe." -ForegroundColor Gray
    Write-Host ""
    Write-Host "To revert later:" -ForegroundColor Gray
    Write-Host "  .\install.ps1 -PrinterHost $ip -Revert" -ForegroundColor Gray
} else {
    Write-Err "Installer exited with code $rc"
    Write-Host "Check messages above. Open an issue if stuck:"
    Write-Host "  https://github.com/grant0013/KAMP-K2/issues"
}
exit $rc
