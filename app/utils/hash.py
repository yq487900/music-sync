import hashlib, os
from pathlib import Path
from tinytag import TinyTag

def compute_hashes(file_path: Path):
    h_md5 = hashlib.md5()
    h_sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h_md5.update(chunk)
            h_sha.update(chunk)
    return h_md5.hexdigest(), h_sha.hexdigest()

def get_duration(file_path: Path):
    try:
        tag = TinyTag.get(str(file_path))
        return tag.duration or 0
    except:
        return 0

def is_existing(track_title, track_artist, duration, local_files):
    for f in local_files:
        if abs((f.duration or 0) - duration) < 2:
            if track_title.lower() in (f.title or "").lower():
                return True
    return False
