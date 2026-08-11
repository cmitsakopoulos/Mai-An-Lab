"""RETIRED — see deprecated_feature/README.md.

Physical-tag writer behind the removed "Edit Metadata" dialog. Its only
caller was StreamripFletApp.apply_metadata_edit, deleted with that dialog.
extract_artwork stayed in StreamripApp/utils/metadata_editor.py — the
now-playing view still uses it.
"""
import os
import logging

try:
    import mutagen
    from mutagen.id3 import ID3, APIC
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.flac import FLAC, Picture
except ImportError:
    mutagen = None

logger = logging.getLogger(__name__)

def update_physical_metadata(file_path, new_tags):
    """
    Uses mutagen to safely overwrite metadata in the physical audio file.
    Supports Title, Artist, Album, Album Artist, Year, and Genre.
    
    new_tags is a dict containing the desired tags.
    """
    if not mutagen:
        logger.error("mutagen library not installed.")
        return False

    if not os.path.exists(file_path):
        logger.error(f"Cannot edit metadata: File missing {file_path}")
        return False

    try:
        # Use easy=True for text tags unification
        f_easy = mutagen.File(file_path, easy=True)
        if f_easy is None:
            logger.error(f"File type not supported by mutagen: {file_path}")
            return False

        # Add tags header if completely missing
        try:
            if f_easy.tags is None:
                f_easy.add_tags()
        except Exception:
            pass

        if 'title' in new_tags: f_easy['title'] = new_tags['title']
        if 'artist' in new_tags: f_easy['artist'] = new_tags['artist']
        if 'album' in new_tags: f_easy['album'] = new_tags['album']
        if 'albumartist' in new_tags: f_easy['albumartist'] = new_tags['albumartist']
        if 'year' in new_tags: f_easy['date'] = str(new_tags['year'])
        if 'genre' in new_tags: f_easy['genre'] = new_tags['genre']
        f_easy.save()

        # Handle artwork separately since easy=True ignores it
        if 'artwork' in new_tags and new_tags['artwork']:
            art_data = new_tags['artwork']
            ext = os.path.splitext(file_path)[1].lower()
            
            if ext == '.mp3':
                try:
                    audio = ID3(file_path)
                except Exception:
                    audio = ID3()
                audio.add(APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,
                    desc='Cover',
                    data=art_data
                ))
                audio.save(file_path, v2_version=3)
            elif ext in ('.mp4', '.m4a', '.m4b'):
                audio = MP4(file_path)
                covr = MP4Cover(art_data, imageformat=MP4Cover.FORMAT_JPEG)
                audio['covr'] = [covr]
                audio.save()
            elif ext == '.flac':
                audio = FLAC(file_path)
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = art_data
                audio.clear_pictures()
                audio.add_picture(pic)
                audio.save()

        logger.info(f"Successfully wrote new tags to {file_path}")
        return True
        
    except PermissionError as pe:
        logger.error(f"Permission denied modifying {file_path}. Android Scoped Storage requires MANAGE_EXTERNAL_STORAGE: {pe}")
        # Return a specific flag so the Flet UI can show a settings prompt
        return "PERMISSION_DENIED" 
    except Exception as e:
        logger.error(f"Failed to write metadata to {file_path}: {e}")
        return False
