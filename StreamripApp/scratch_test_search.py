import sys
import os
import time
import logging
import asyncio

logging.basicConfig(level=logging.INFO)  # Change to INFO for cleaner console output

# Add current directory to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from utils.streamrip_search import StreamripSearcher
from utils.streamrip_api import load_config

query = sys.argv[1] if len(sys.argv) > 1 else "love"

# Load and print configuration to debug TOML functionality
try:
    cfg = load_config()
    q = cfg.get("qobuz", {})
    print("------------------------------")
    print("LOADED CONFIGURATION DETAILS:")
    print("------------------------------")
    print(f"use_auth_token:    {q.get('use_auth_token')}")
    print(f"email_or_userid:   {q.get('email_or_userid')}")
    token = q.get('password_or_token', '')
    masked_token = (token[:6] + "..." + token[-6:]) if len(token) > 12 else ("..." if token else "None")
    print(f"password_or_token: {masked_token}")
    print(f"app_id:            {q.get('app_id')}")
    print(f"secrets count:     {len(q.get('secrets', []))}")
    print("------------------------------\n")
except Exception as e:
    print(f"Error reading configuration: {e}")

searcher = StreamripSearcher()

results_received = None
def callback(results):
    global results_received
    results_received = results

def progress_callback(status, detail):
    print(f"[PROGRESS] {status}: {detail}")

print(f"Starting Qobuz search for: '{query}'...")
searcher.search(query, "qobuz", callback, progress_callback=progress_callback)

# Wait up to 15 seconds for results
for _ in range(150):
    if results_received is not None:
        break
    time.sleep(0.1)

print("\n------------------------------")
print("RESULTS RECEIVED:")
print("------------------------------")
if results_received is None:
    print("Error: Search timed out (no response received).")
elif isinstance(results_received, dict) and "error" in results_received:
    print(f"Error returned: {results_received['error']}")
else:
    print(f"Successfully retrieved {len(results_received)} results:")
    import pprint
    pprint.pprint(results_received[:5]) # Show first 5 results for brevity
    if len(results_received) > 5:
        print(f"... and {len(results_received) - 5} more items.")

