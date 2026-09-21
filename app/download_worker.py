import asyncio
from pathlib import Path
from app.config import config as cfg
from app.sources.luoshe import LuosheSource
from app.utils.hash import compute_hashes, get_duration

async def download_one(title, artist):
    source = LuosheSource()
    tracks = await source.search(title, artist)
    if not tracks:
        return False
    track = tracks[0]
    dest = Path(cfg["download_dir"]) / f"{artist} - {title}.mp3"
    dest.parent.mkdir(parents=True, exist_ok=True)
    ok = await source.download(track, str(dest))
    return ok

if __name__ == "__main__":
    asyncio.run(download_one("测试", "测试艺术家"))
