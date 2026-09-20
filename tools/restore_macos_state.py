#!/usr/bin/env python3
"""Restores macOS state files from ~/.mai_an_lab_pre_lobotomy_backup."""
import os
import shutil

backup_dir = os.path.expanduser('~/.mai_an_lab_pre_lobotomy_backup')
if not os.path.exists(backup_dir):
    print(f"Error: Backup directory {backup_dir} not found.")
    exit(1)

home = os.path.expanduser('~')
app_support = os.path.expanduser('~/Library/Application Support/streamrip')
os.makedirs(app_support, exist_ok=True)

restore_map = {
    os.path.join(backup_dir, '.onboarded'): os.path.join(home, '.onboarded'),
    os.path.join(backup_dir, 'library.db'): os.path.join(home, 'library.db'),
    os.path.join(backup_dir, 'flet_prefs.json'): os.path.join(home, 'flet_prefs.json'),
    os.path.join(backup_dir, 'queue_state.json'): os.path.join(home, 'queue_state.json'),
    os.path.join(backup_dir, 'app_support_config.toml'): os.path.join(app_support, 'config.toml'),
    os.path.join(backup_dir, 'app_support_library.db'): os.path.join(app_support, 'library.db'),
    os.path.join(backup_dir, 'app_support_user_prefs.json'): os.path.join(app_support, 'user_prefs.json'),
    os.path.join(backup_dir, 'failed_downloads.db'): os.path.join(app_support, 'failed_downloads.db'),
    os.path.join(backup_dir, 'recent_searches.json'): os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'StreamripApp', 'recent_searches.json'),
    os.path.join(backup_dir, 'graph_state.json'): os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'StreamripApp', 'graph_state.json'),
}

for src, dst in restore_map.items():
    if os.path.exists(src):
        shutil.copy2(src, dst)
        print(f"Restored: {src} -> {dst}")

pca_src = os.path.join(backup_dir, 'pca_report')
pca_dst = os.path.join(app_support, 'pca_report')
if os.path.exists(pca_src):
    if os.path.exists(pca_dst):
        shutil.rmtree(pca_dst)
    shutil.copytree(pca_src, pca_dst)
    print(f"Restored directory: {pca_src} -> {pca_dst}")

flet_src = os.path.join(backup_dir, 'flet_storage_data')
flet_dst = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'StreamripApp', '.flet', 'storage', 'data')
if os.path.exists(flet_src):
    os.makedirs(flet_dst, exist_ok=True)
    for item in os.listdir(flet_src):
        s = os.path.join(flet_src, item)
        d = os.path.join(flet_dst, item)
        if os.path.isdir(s):
            if os.path.exists(d):
                shutil.rmtree(d)
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
    print(f"Restored directory: {flet_src} -> {flet_dst}")

print("State restored successfully.")
