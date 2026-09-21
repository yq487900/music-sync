import aiohttp, aiofiles
from .base import MusicSource, TrackInfo

class LuosheSource(MusicSource):
    name = "luoshe"
    def __init__(self, base_url="https://music.52pojie.cn"):
        self.base_url = base_url.rstrip("/")
    async def search(self, title: str, artist: str):
        url = f"{self.base_url}/api/search"
        params = {"keywords": f"{artist} {title}", "type": 1}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        results = []
        for item in data.get("data", {}).get("songs", []):
            results.append(TrackInfo(
                title=item.get("name"),
                artist=",".join([a.get("name") for a in item.get("artists", [])]),
                duration=item.get("duration", 0)/1000,
                url=f"{self.base_url}/api/song/url?id={item.get('id')}",
                bitrate=320,
                format="mp3",
                source=self.name
            ))
        return results
    async def download(self, track: TrackInfo, dest_path: str):
        async with aiohttp.ClientSession() as s:
            async with s.get(track.url) as r:
                if r.status != 200:
                    return False
                data = await r.read()
        async with aiofiles.open(dest_path, "wb") as f:
            await f.write(data)
        return True
