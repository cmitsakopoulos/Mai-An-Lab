"""
This file contains code from streamrip (https://github.com/nathom/streamrip).
Streamrip is the property of nathom and multiple other contributors in the streamrip community.
Big thanks to nathom and the streamrip community for their incredible work.
"""
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class AlbumMetadata:
    id: str
    album: str
    artist: str
    year: str
    tracktotal: int
    disctotal: int
    genres: Optional[List[str]] = None
    copyright: Optional[str] = None
    
    def get_genres(self) -> str:
        return ", ".join(self.genres) if self.genres else ""
        
    def get_copyright(self) -> str:
        return self.copyright or ""

@dataclass
class TrackMetadata:
    id: str
    title: str
    artist: str
    album: AlbumMetadata
    tracknumber: int
    discnumber: int
    isrc: Optional[str] = None
    composer: Optional[str] = None
    lyrics: Optional[str] = None
