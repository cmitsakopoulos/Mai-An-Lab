import os
import sys
from tomlkit import parse, dumps
import logging
import asyncio
import time
import shutil
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TYER
from pathlib import Path

from .config import Config, DEFAULT_CONFIG_PATH, CURRENT_CONFIG_VERSION
from .qobuz import QobuzClient
from .downloadable import BasicDownloadable
from .filepath_utils import clean_filename
from .metadata_objects import AlbumMetadata, TrackMetadata
from .tagger import tag_file

logger = logging.getLogger(__name__)

def get_platform_name():
    if hasattr(sys, 'getandroidapilevel'): return 'android'
    elif sys.platform == 'win32': return 'win'
    elif sys.platform == 'darwin': return 'macosx'
    return 'linux'

def get_config_path():
    """Absolute path to the LIVE, user-writable config file.

    This MUST be a persistent, writable location — never the in-package
    template. The template (`BLANK_CONFIG_PATH` == ``<pkg>/utils/config.toml``)
    ships inside the app bundle and is re-extracted (reset to defaults) on every
    (re)install; the previous implementation returned that template whenever it
    existed — which is always, since it's asserted present at import — so the
    "live" config lived in the read-only, reinstall-replaced code directory.
    That's why user settings broke on repeated installs while the DB (which
    lives under ``get_app_dir()``) survived.

    We resolve to the canonical ``DEFAULT_CONFIG_PATH`` (under the persistent app
    dir, alongside library.db), so config shares the DB's persistence and is
    captured/restored by the state-bundle export/import. An explicit
    ``STREAMRIP_CONFIG_PATH`` env var overrides this for dev convenience (e.g.
    pointing at a repo-local config when running from source)."""
    override = os.getenv("STREAMRIP_CONFIG_PATH")
    if override:
        parent = os.path.dirname(os.path.abspath(override))
        os.makedirs(parent, exist_ok=True)
        return override

    os.makedirs(os.path.dirname(DEFAULT_CONFIG_PATH), exist_ok=True)
    return DEFAULT_CONFIG_PATH

def get_default_download_path():
    plat = get_platform_name()
    if plat == 'android':
        base_dir = os.getenv("FLET_APP_STORAGE_DATA") or os.getenv("APP_FILES_PATH") or "/data/user/0"
        return os.path.join(base_dir, "StreamripDownloads")
    return os.path.join(os.path.expanduser("~"), "Music", "Streamrip")

def ensure_config_exists():
    path = get_config_path()
    from .config import BLANK_CONFIG_PATH
    
    if not os.path.exists(path):
        # Create from template if missing
        shutil.copy(BLANK_CONFIG_PATH, path)
        logger.info(f"Created default config from template at {path}")
        
        # Inject default download path
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = parse(f.read())
            doc["downloads"]["folder"] = ""
            with open(path, "w", encoding="utf-8") as f:
                f.write(dumps(doc))
        except Exception as e:
            logger.error(f"Failed to set empty download path: {e}")

    # Repair when the version is stale OR the on-disk config is missing sections
    # the current template defines. The structural check is not redundant: a
    # config can share the current version yet still be incomplete — e.g. one
    # written from an older, partial template that lacked whole streamrip sections
    # (qobuz/tidal/…) and [downloads] fields ConfigData now requires. Such a
    # config crashes ConfigData.from_toml with "missing N positional arguments",
    # and a version-only gate never fires (the versions already match), so it
    # stays broken forever.
    #
    # The repair is ADDITIVE (fill_missing), not template-authoritative: the live
    # config mixes streamrip sections with app-only sections the streamrip
    # template never defines (general/appearance/landing). update_config /
    # Config.update_file would wipe those app sections — and any Qobuz/Tidal
    # credentials living under a section the template rewrites — so we only add
    # what's absent and touch nothing the user already set.
    try:
        from .config import BLANK_CONFIG_PATH, fill_missing, toml_set_user_defaults

        with open(path, "r", encoding="utf-8") as f:
            doc = parse(f.read())
        with open(BLANK_CONFIG_PATH, "r", encoding="utf-8") as f:
            template_doc = parse(f.read())
        # Machine-specific defaults (download DB paths, youtube folder) so any
        # section we fill in points at the right place instead of a blank/foreign
        # path.
        toml_set_user_defaults(template_doc)

        current_v = doc.get("misc", {}).get("version")
        added = fill_missing(doc, template_doc)

        if added or current_v != CURRENT_CONFIG_VERSION:
            if "misc" not in doc:
                doc["misc"] = {}
            doc["misc"]["version"] = CURRENT_CONFIG_VERSION
            with open(path, "w", encoding="utf-8") as f:
                f.write(dumps(doc))
            if added:
                logger.info("Repaired config: filled in missing sections/keys from template")
            else:
                logger.info(f"Migrated config version {current_v} -> {CURRENT_CONFIG_VERSION}")
    except Exception as e:
        logger.error(f"Config migration failed: {e}")

def load_config():
    ensure_config_exists()
    try:
        with open(get_config_path(), "r", encoding="utf-8") as f:
            return parse(f.read())
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}

def update_config_params(params):
    path = get_config_path()
    ensure_config_exists()
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = parse(f.read())
        
        for section, values in params.items():
            # If section contains underscores, it might be a flat key from main.py
            # e.g. "general_startup_page"
            if "_" in section and section not in cfg:
                parts = section.split("_", 1)
                real_section = parts[0]
                real_key = parts[1]
                if real_section not in cfg: cfg[real_section] = {}
                cfg[real_section][real_key] = values
            else:
                if section not in cfg: cfg[section] = {}
                if isinstance(values, dict):
                    cfg[section].update(values)
                else:
                    # Flat update for cases where section is a string
                    pass
        
        with open(path, 'w', encoding="utf-8") as f:
            f.write(dumps(cfg))
        return True
    except Exception as e:
        logger.error(f"Failed to update config: {e}")
        return False

def repair_config():
    ensure_config_exists()
    return True

def _detect_type_and_id(url: str):
    # Minimal URL parser for Qobuz
    patterns = [
        (r'qobuz\.com/.*?track/(?P<id>\w+)', 'track'),
        (r'qobuz\.com/.*?album/(?P<id>\w+)', 'album'),
        (r'qobuz\.com/.*?artist/(?P<id>\w+)', 'artist'),
        (r'qobuz\.com/.*?playlist/(?P<id>\w+)', 'playlist'),
    ]
    for pattern, mtype in patterns:
        match = re.search(pattern, url)
        if match: return mtype, match.group('id')
    return None, None

import re

async def download(url, download_dir, progress_callback=None, quality=None, stop_event=None):
    """Qobuz-only download entry point with granular progress tracking."""
    os.makedirs(download_dir, exist_ok=True)
    
    mtype, mid = _detect_type_and_id(url)
    if not mtype:
        raise Exception(f"Could not parse Qobuz URL: {url}")

    config = Config(get_config_path())
    client = QobuzClient(config)
    
    import tempfile
    await client.login()

    if progress_callback:
        progress_callback({"status": "Initializing", "percent": 0, "message": "Fetching metadata..."})

    meta = await client.get_metadata(mid, mtype)
    
    tracks = []
    if mtype == 'track':
        tracks = [meta]
    elif mtype == 'album':
        album_meta = meta
        tracks = meta.get('tracks', {}).get('items', [])
        for t in tracks:
            if 'album' not in t: t['album'] = album_meta
    elif mtype == 'playlist':
        tracks = meta.get('tracks', {}).get('items', [])
    elif mtype == 'artist':
        albums = meta.get('albums', {}).get('items', [])
        for a in albums:
            a_meta = await client.get_metadata(str(a['id']), 'album')
            a_tracks = a_meta.get('tracks', {}).get('items', [])
            for t in a_tracks:
                if 'album' not in t: t['album'] = a_meta
            tracks.extend(a_tracks)

    if not tracks:
        return []

    if progress_callback:
        progress_callback({"status": "Preparing", "percent": 0, "message": "Resolving stream URLs..."})

    # Resolve all downloadables and their sizes first
    resolve_sem = asyncio.Semaphore(10)
    
    async def _resolve(t_meta):
        async with resolve_sem:
            try:
                target_quality = quality if quality is not None else config.session.qobuz.quality
                d = await client.get_downloadable(str(t_meta['id']), target_quality)
                await d.size()
                return d
            except Exception as e:
                logger.warning(f"Could not resolve track {t_meta.get('id')}: {e}")
                return None

    dl_results = await asyncio.gather(*[_resolve(t) for t in tracks])
    active_jobs = []
    total_bytes = 0
    
    for i, d in enumerate(dl_results):
        if d:
            active_jobs.append((tracks[i], d))
            total_bytes += d._size or 0

    if not active_jobs:
        raise Exception("No tracks could be resolved for download.")

    downloaded_bytes = 0
    completed_tracks = 0
    last_update_time = [0.0]
    
    max_connections = config.session.downloads.max_connections if config.session.downloads.max_connections > 0 else 6
    dl_sem = asyncio.Semaphore(max_connections)

    def _trigger_update(status="Downloading"):
        now = time.time()
        if now - last_update_time[0] < 0.1:
            return
        last_update_time[0] = now
        
        if progress_callback:
            pct = (downloaded_bytes / total_bytes * 100) if total_bytes > 0 else 0
            msg = f"{downloaded_bytes/1e6:.1f}/{total_bytes/1e6:.1f} MB • {completed_tracks}/{len(active_jobs)} tracks"
            progress_callback({
                "status": status,
                "percent": int(pct),
                "message": msg
            })

    async def _download_track(i, track_meta, downloadable):
        nonlocal downloaded_bytes, completed_tracks
        if stop_event and stop_event.is_set():
            return
            
        async with dl_sem:
            track_id = str(track_meta['id'])
            title = track_meta.get('title', 'Unknown')
            
            try:
                track_num = track_meta.get('track_number', i+1)
                raw_filename = f"{track_num:02d}. {title}.{downloadable.extension}"
                filename = clean_filename(raw_filename, restrict=False)
                dest_path = os.path.join(download_dir, filename)
                
                def _chunk_cb(chunk_len):
                    nonlocal downloaded_bytes
                    downloaded_bytes += chunk_len
                    _trigger_update()
                
                await downloadable.download(dest_path, _chunk_cb)
                
                _trigger_update("Tagging")
                
                album_meta_data = track_meta.get('album', {})
                image_raw = album_meta_data.get('image')
                cover_url = ""
                if isinstance(image_raw, str):
                    cover_url = image_raw
                elif isinstance(image_raw, dict):
                    cover_url = image_raw.get('large') or image_raw.get('extralarge') or image_raw.get('medium') or image_raw.get('small') or ""

                cover_path = None
                if cover_url and cover_url.startswith("http"):
                    cover_path = os.path.join(tempfile.gettempdir(), f"__cover_{track_id}.jpg")
                    try:
                        async with client.session.get(cover_url) as resp:
                            if resp.status == 200:
                                with open(cover_path, "wb") as f:
                                    f.write(await resp.read())
                    except Exception:
                        cover_path = None

                artist_dict = album_meta_data.get('artist', {})
                album_artist = artist_dict.get('name', 'Unknown Artist') if isinstance(artist_dict, dict) else str(artist_dict)

                genre_data = album_meta_data.get('genre', {})
                genres = [g.get('name') for g in genre_data.get('path', []) if isinstance(g, dict)] if isinstance(genre_data, dict) else []
                        
                album_obj = AlbumMetadata(
                    id=str(album_meta_data.get('id', '')),
                    album=album_meta_data.get('title', 'Unknown Album'),
                    artist=album_artist,
                    year=str(album_meta_data.get('release_date', '')).split('-')[0][:4],
                    tracktotal=album_meta_data.get('tracks_count', 1),
                    disctotal=album_meta_data.get('media_count', 1),
                    genres=genres if genres else None,
                    copyright=album_meta_data.get('copyright')
                )
                
                track_artist = track_meta.get('performer', {}).get('name') or track_meta.get('artist', {}).get('name') or 'Unknown Artist'
                composer_dict = track_meta.get('composer', {})
                
                track_obj = TrackMetadata(
                    id=str(track_id),
                    title=title,
                    artist=track_artist,
                    album=album_obj,
                    tracknumber=track_num,
                    discnumber=track_meta.get('media_number', 1),
                    isrc=track_meta.get('isrc'),
                    composer=composer_dict.get('name') if isinstance(composer_dict, dict) else None,
                    lyrics=None
                )

                await tag_file(dest_path, track_obj, cover_path)
                if cover_path and os.path.exists(cover_path):
                    os.remove(cover_path)

                # Make file globally visible and editable to other Android apps
                if get_platform_name() == 'android':
                    def _fix_perms():
                        try:
                            os.chmod(dest_path, 0o666)
                            # Fix parent directory permissions if we created it
                            parent = os.path.dirname(dest_path)
                            if parent and parent != "/":
                                os.chmod(parent, 0o777)
                            import subprocess
                            # Try legacy broadcast scanner
                            subprocess.run(["am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{dest_path}"], capture_output=True)
                            # Try Android 11+ content provider insertion to force a scan
                            subprocess.run(["content", "insert", "--uri", "content://media/external/file", "--bind", f"_data:s:{dest_path}"], capture_output=True)
                        except Exception:
                            pass
                    await asyncio.to_thread(_fix_perms)

            except Exception as e:
                logger.error(f"Failed to download track {track_id}: {e}")
                
            finally:
                completed_tracks += 1
                _trigger_update()

    tasks = [_download_track(i, t_meta, d_able) for i, (t_meta, d_able) in enumerate(active_jobs)]
    if tasks:
        await asyncio.gather(*tasks)

    if progress_callback:
        progress_callback({"status": "Finished", "percent": 100, "message": "Download complete."})
    
    await client.session.close()
    return []
