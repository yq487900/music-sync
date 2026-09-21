import aiohttp

class NeteaseSync:
    def __init__(self, cookie=""):
        self.cookie = cookie
        self.base = "https://music.163.com"
    async def get_user_playlists(self, user_id):
        url = f"{self.base}/api/user/playlist?uid={user_id}"
        headers = {"Cookie": self.cookie, "Referer": self.base}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200:
                    return []
                return await r.json().get("playlist", [])
    async def get_playlist_tracks(self, playlist_id):
        url = f"{self.base}/api/playlist/track?limit=1000&id={playlist_id}"
        headers = {"Cookie": self.cookie, "Referer": self.base}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                return data.get("songs", [])
