$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    py -3 -m venv .venv
}
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-build.txt

if (Test-Path 'build') { Remove-Item -Recurse -Force 'build' }
if (Test-Path 'dist\GPTAutomation') { Remove-Item -Recurse -Force 'dist\GPTAutomation' }

.venv\Scripts\pyinstaller.exe `
  --noconfirm `
  --clean `
  --onedir `
  --windowed `
  --name GPTAutomation `
  --add-data 'frontend\dist;frontend\dist' `
  --add-data '.env.example;.' `
  --add-data 'VERSION;.' `
  --collect-all cloakbrowser `
  desktop_app.py

if (Get-Command iscc -ErrorAction SilentlyContinue) {
    iscc packaging\GPTAutomation.iss
} else {
    Write-Warning 'Inno Setup (iscc.exe) is not installed; PyInstaller output is available at dist\GPTAutomation.'
}
