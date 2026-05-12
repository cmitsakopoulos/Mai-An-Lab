import json
import os
from datetime import datetime

def json_serial(obj):
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)

def safe_json_dump(data, fh, indent=None):
    json.dump(data, fh, indent=indent, default=json_serial)
import sys
IS_ANDROID = hasattr(sys, 'getandroidapilevel')

def get_history_path():
    if IS_ANDROID:
        base_dir = os.getenv("FLET_APP_STORAGE_DATA") or os.getenv("APP_FILES_PATH") or "/data/user/0"
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, 'history.json')

def load_history():
    path = get_history_path()
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def save_history(history):
    path = get_history_path()
    try:
        with open(path, 'w') as f:
            safe_json_dump(history, f, indent=4)
    except Exception as e:
        print(f"Failed to save history: {e}")

def add_to_history(item_data):
    history = load_history()
    
    # Store essential metadata for the Luxury UI
    entry = {
        'name': item_data.get('name', 'Unknown'),
        'artist': item_data.get('artist', 'Unknown Artist'),
        'source': item_data.get('source', 'qobuz'),
        'image': item_data.get('image', ''),
        'year': item_data.get('year', 'N/A'),
        'media_type': item_data.get('media_type', 'track'),
        'url': item_data.get('url', ''),
        'date': datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    
    # Avoid duplicates in history
    history = [h for h in history if h.get('url') != entry['url']]
    
    history.insert(0, entry)
    history = history[:100] # Increase history buffer for power users
    save_history(history)
