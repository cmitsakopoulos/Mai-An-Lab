# 1. Resolve local paths
python configure_paths.py

# 2. Run build
Write-Host "Starting incremental Flet build..." -ForegroundColor Cyan
Stop-Process -Name "java" -Force -ErrorAction SilentlyContinue 
flet build apk -v --yes 
adb install -r build/apk/mai-an-lab.apk
