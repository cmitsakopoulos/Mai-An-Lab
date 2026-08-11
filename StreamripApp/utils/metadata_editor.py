import os
import logging

try:
    import mutagen
    # Picture is the only symbol extract_artwork needs directly (the ogg/opus
    # branch decodes a metadata_block_picture); every other format is read
    # through the generic mutagen.File tag interface.
    from mutagen.flac import Picture
except ImportError:
    mutagen = None

logger = logging.getLogger(__name__)

def extract_artwork(file_path):
    """
    Extracts the first embedded artwork from the audio file using mutagen.
    Returns raw bytes or None.
    """
    if not mutagen: return None
    try:
        f = mutagen.File(file_path)
        if f is None:
            return None

        ext = os.path.splitext(file_path)[1].lower()

        if ext == '.mp3':
            if f.tags:
                for key in f.tags.keys():
                    if key.startswith('APIC'):
                        return f.tags[key].data
        elif ext in ('.mp4', '.m4a', '.m4b'):
            if f.tags and 'covr' in f.tags and f.tags['covr']:
                return bytes(f.tags['covr'][0])
        elif ext == '.flac':
            if f.pictures:
                return f.pictures[0].data
        elif ext in ('.ogg', '.opus'):
            if f.get('metadata_block_picture'):
                import base64
                pic_data = f['metadata_block_picture'][0]
                pic = Picture(base64.b64decode(pic_data))
                return pic.data
                
    except Exception as e:
        logger.warning(f"Could not extract artwork from {file_path}: {e}")
    return None
