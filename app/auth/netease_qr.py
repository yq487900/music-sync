import aiohttp, asyncio, time, json
from typing import Optional, Dict

class NeteaseQR:
    BASE = "https://music.163.com"
    def __init__(self):
        self.session = None
    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def get_qr_key(self) -> str:
        s = await self._get_session()
        async with s.get(f"{self.BASE}/api/qr/key") as r:
            data = await r.json()
            return data.get("data", {}).get("unikey", "")

    async def create_qr(self, key: str) -> str:
        s = await self._get_session()
        params = {"key": key, "qrimg": "true"}
        async with s.get(f"{self.BASE}/api/qr/create", params=params) as r:
            data = await r.json()
            return data.get("data", {}).get("qrurl", "")

    async def check_qr(self, key: str) -> Dict:
        s = await self._get_session()
        params = {"key": key}
        async with s.get(f"{self.BASE}/api/qr/check", params=params) as r:
            return await r.json()

    async def login_wait(self, timeout: int = 120) -> Optional[str]:
        key = await self.get_qr_key()
        qr_url = await self.create_qr(key)
        start = time.time()
        while time.time() - start < timeout:
            res = await self.check_qr(key)
            code = res.get("code")
            if code == 803:  # success
                cookie = res.get("cookie", "")
                return cookie
            elif code == 800:  # expired
                break
            await asyncio.sleep(2)
        return None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()