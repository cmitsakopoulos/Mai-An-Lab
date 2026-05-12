#!/bin/bash
# 1. Resolve local paths
python3 configure_paths.py

# 2. Run build
echo "Starting Flet build..."
flet build apk --clear-cache -v
