import json
import os

def json_serial(obj):
    if isinstance(obj, set):
        return list(obj)
    return str(obj)

def safe_json_dump(data, fh, indent=None):
    json.dump(data, fh, indent=indent, default=json_serial)
import sys
IS_ANDROID = hasattr(sys, 'getandroidapilevel')

def get_search_history_path():
    if IS_ANDROID:
        base_dir = os.getenv("FLET_APP_STORAGE_DATA") or os.getenv("APP_FILES_PATH") or "/data/user/0"
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, 'recent_searches.json')

def load_searches():
    path = get_search_history_path()
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def add_search(url):
    if not url: return
    searches = load_searches()
    
    # Remove if already exists to move to top
    if url in searches:
        searches.remove(url)
    
    searches.insert(0, url)
    searches = searches[:10] # Keep last 10
    
    path = get_search_history_path()
    try:
        with open(path, 'w') as f:
            safe_json_dump(searches, f, indent=4)
    except Exception as e:
        print(f"Failed to save searches: {e}")
