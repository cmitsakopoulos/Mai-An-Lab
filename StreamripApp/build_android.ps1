# 1. Resolve local paths
python configure_paths.py

# 2. Run build
Write-Host "Starting Flet build..." -ForegroundColor Cyan
flet build apk --clear-cache -v
