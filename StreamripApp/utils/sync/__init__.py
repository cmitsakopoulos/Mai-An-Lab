"""
Mai-An Lab Device-to-Device Sync & DSP Offloading Module.
Enables local network communication between macOS and Android Flet apps.
"""
from .discovery import BeaconBroadcaster, BeaconListener, resolve_local_ip, UDP_PORT
from .transcoder import transcode_audio, TranscodeProfile
from .server import CompanionServer
from .client import CompanionClient

__all__ = [
    "BeaconBroadcaster",
    "BeaconListener",
    "resolve_local_ip",
    "UDP_PORT",
    "transcode_audio",
    "TranscodeProfile",
    "CompanionServer",
    "CompanionClient",
]

