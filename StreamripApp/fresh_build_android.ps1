# 1. Resolve local paths
python configure_paths.py

# 2. Run build
Write-Host "Starting fresh Flet build..." -ForegroundColor Cyan
Stop-Process -Name "java" -Force -ErrorAction SilentlyContinue 
Write-Host "Wiping previous build directory: $((Get-Location).Path)\build" -ForegroundColor Yellow
Remove-Item -Path "build", ".gradle" -Recurse -Force -ErrorAction SilentlyContinue  
flet build apk --clear-cache -v --yes 
adb uninstall com.mitsakopoulos.maianlab.mai_an_lab
adb install build/apk/mai-an-lab.apk