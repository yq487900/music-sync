from abc import ABC, abstractmethod
from typing import List, Dict, Any

class TrackInfo:
    def __init__(self, title, artist, duration, url, bitrate, format, source):
        self.title = title
        self.artist = artist
        self.duration = duration
        self.url = url
        self.bitrate = bitrate
        self.format = format
        self.source = source
        self.quality_score = (bitrate or 0) * (1 if format.lower() in ['flac','wav'] else 0.8)

class MusicSource(ABC):
    name: str
    @abstractmethod
    async def search(self, title: str, artist: str) -> List[TrackInfo]:
        pass
    @abstractmethod
    async def download(self, track: TrackInfo, dest_path: str) -> bool:
        pass
