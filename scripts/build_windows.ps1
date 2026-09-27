param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $ProjectRoot
try {
    & $Python -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }
    & $Python -m PyInstaller --noconfirm --distpath dist/updated voxlate.spec
    if ($LASTEXITCODE -ne 0) { throw "EXE build failed." }
    Write-Host "Ready: $ProjectRoot\dist\updated\voxlate.exe"
} finally {
    Pop-Location
}
