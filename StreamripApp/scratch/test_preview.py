import os
import sys
import asyncio
import subprocess

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import Config
from utils.qobuz import QobuzClient
from utils.streamrip_search import StreamripSearcher

async def main():
    # Let's find config path
    from main import get_app_dir
    app_dir = get_app_dir()
    print("get_app_dir():", app_dir)
    
    from utils.streamrip_api import get_config_path
    config_path = get_config_path()
    print("get_config_path():", config_path)
    
    if not os.path.exists(config_path):
        print(f"Config path {config_path} does not exist!")
        return
        
    config = Config(config_path)
    qobuz_cfg = config.session.qobuz
    print("Qobuz Email/UserID:", qobuz_cfg.email_or_userid)
    print("Qobuz Has Password/Token:", bool(qobuz_cfg.password_or_token))
    
    # Instantiate client and login
    client = QobuzClient(config)
    try:
        await client.login()
        print("Login Successful!")
    except Exception as e:
        print("Login Failed:", e)
        return
        
    # Search for a track to get its ID
    query = "pink floyd time"
    print(f"Searching for '{query}'...")
    results = await client.search("track", query, limit=5)
    
    # Get tracks
    tracks = results[0].get("tracks", {}).get("items", []) if results else []
    if not tracks:
        print("No tracks found.")
        await client.session.close()
        return
        
    track = tracks[0]
    track_id = str(track["id"])
    title = track.get("title", "Unknown")
    artist = track.get("performer", {}).get("name") or "Unknown"
    print(f"Found track: {title} by {artist} (ID: {track_id})")
    
    # Get direct downloadable stream URL
    print("Fetching downloadable stream URL...")
    try:
        # quality=1 maps to MP3 128kbps (lowest quality)
        downloadable = await client.get_downloadable(track_id, quality=1)
        print("Successfully resolved stream URL!")
        print("Stream URL:", downloadable.url)
        print("File extension:", downloadable.extension)
        print("Source:", downloadable.source)
        
        # Now, play the stream for 5 seconds with ffplay to verify
        print("\n--> Playing stream via ffplay (macOS native audio output) for 5 seconds...")
        cmd = ["/opt/homebrew/bin/ffplay", "-nodisp", "-autoexit", "-t", "5", downloadable.url]
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.wait()
        print("--> Playback finished/terminated.")
        
    except Exception as e:
        print("Failed during stream play test:", e)
        
    await client.session.close()

if __name__ == "__main__":
    asyncio.run(main())
