import os
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, APIC
from pathlib import Path

def write_tags(file_path: Path, title: str, artist: str, album: str, cover_url: str = None):
    try:
        audio = ID3(file_path)
    except:
        audio = ID3()
    audio.add(TIT2(encoding=3, text=title))
    audio.add(TPE1(encoding=3, text=artist))
    if album:
        audio.add(TALB(encoding=3, text=album))
    audio.save(file_path)
