# KAMP-K2 bootstrap -- iex-compatible entrypoint.
#
# `install.ps1` declares [CmdletBinding()] + param(), which PowerShell only
# accepts at the top of a script FILE. `Invoke-Expression` can't parse those
# constructs in an expression block, so piping install.ps1 through iex fails
# with "Unexpected attribute 'CmdletBinding'".
#
# This bootstrap is flat top-level code (no param block, no CmdletBinding),
# so `iwr | iex` works. It downloads install.ps1 to a temp file and runs it
# as a real script.
#
# One-liner for users:
#
#   iwr -useb https://raw.githubusercontent.com/grant0013/KAMP-K2/main/bootstrap.ps1 | iex

$InstallUrl = "https://raw.githubusercontent.com/grant0013/KAMP-K2/main/install.ps1"
$TargetPath = Join-Path $env:TEMP "kamp-k2-install.ps1"

Write-Host ""
Write-Host "[*] Downloading KAMP-K2 installer..." -ForegroundColor Cyan
try {
    Invoke-WebRequest -UseBasicParsing -Uri $InstallUrl -OutFile $TargetPath -ErrorAction Stop
} catch {
    Write-Host "[x] Download failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Check your internet connection or open an issue:" -ForegroundColor Yellow
    Write-Host "  https://github.com/grant0013/KAMP-K2/issues" -ForegroundColor Yellow
    exit 1
}
Write-Host "[+] Saved to $TargetPath" -ForegroundColor Green
Write-Host "[*] Launching installer..." -ForegroundColor Cyan
Write-Host ""

# Relaunch install.ps1 as a real script so its param() block and
# [CmdletBinding()] are honoured. Wrap in try/catch so ANY error surfaces
# to the user -- silent exits were a real reported issue.
try {
    & $TargetPath
    $rc = $LASTEXITCODE
} catch {
    Write-Host ""
    Write-Host "[x] Installer crashed with an error:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Script location: $TargetPath" -ForegroundColor Yellow
    Write-Host "Stack trace:" -ForegroundColor Yellow
    Write-Host $_.ScriptStackTrace -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Paste the above into a GitHub issue:" -ForegroundColor Yellow
    Write-Host "  https://github.com/grant0013/KAMP-K2/issues" -ForegroundColor Yellow
    exit 1
}
exit $rc
