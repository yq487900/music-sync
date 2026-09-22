"""网易云扫码登录。

走自建 ncm-api，而不是直连 music.163.com —— 网页直连通道拿不到登录凭证
（扫码后一直停在「等待扫码」），ncm-api 会在 803 时直接带回 Set-Cookie。
"""
from __future__ import annotations

import io
from typing import Dict

from app.ncm import (QR_EXPIRED, QR_OK, QR_SCANNED, QR_WAITING, Ncm, NcmError,
                     extract_music_u)


def _cookie_from_music_u(music_u: str) -> str:
    return f"MUSIC_U={music_u}; os=pc"


class NeteaseQR:
    """保留原类名/方法名，便于上层沿用"""

    @staticmethod
    async def create() -> Dict[str, str]:
        """创建二维码：返回 key 与可直接渲染的二维码地址"""
        ncm = Ncm()
        try:
            key = await ncm.qr_key()
            if not key:
                return {"key": "", "qr_url": "", "qrurl": ""}
            data = await ncm.qr_create(key)
        finally:
            await ncm.close()
        qr_url = data.get("qrimg") or ""
        if not qr_url:
            # 兜底：ncm-api 没给图片时本地生成
            qr_url = f"/api/login/netease/qr.png?key={key}"
        return {"key": key, "qr_url": qr_url, "qrurl": data.get("qrurl") or ""}

    @staticmethod
    def qr_png(url: str) -> bytes:
        """本地生成二维码 PNG（仅作为兜底）"""
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    async def check(key: str) -> Dict:
        """单次查询扫码状态"""
        ncm = Ncm()
        try:
            res = await ncm.qr_check(key)
        except NcmError as e:
            return {"code": QR_WAITING, "message": f"查询失败: {e}", "cookie": "", "user_id": ""}
        finally:
            await ncm.close()

        code = res.get("code")
        out = {"code": code, "message": res.get("message") or "", "cookie": "", "user_id": ""}
        if code != QR_OK:
            return out

        music_u = extract_music_u(res.get("cookie") or "")
        if not music_u:
            out["message"] = "已确认但未返回登录凭证，请重新扫码"
            return out

        cookie = _cookie_from_music_u(music_u)
        try:
            out["user_id"] = await NeteaseQR.get_user_id(cookie)
        except Exception:  # noqa: BLE001
            out["user_id"] = ""
        out["cookie"] = cookie
        out["message"] = "登录成功"
        return out

    @staticmethod
    async def get_user_id(cookie: str) -> str:
        """用已登录 Cookie 取用户 ID（同步歌单必需）"""
        ncm = Ncm(cookie=cookie)
        try:
            return await ncm.user_id()
        finally:
            await ncm.close()

    @staticmethod
    async def logged_in(cookie: str) -> bool:
        ncm = Ncm(cookie=cookie)
        try:
            return await ncm.logged_in()
        finally:
            await ncm.close()


# 状态码别名，供上层引用
CODE_EXPIRED: int = QR_EXPIRED
CODE_WAITING: int = QR_WAITING
CODE_SCANNED: int = QR_SCANNED
CODE_OK: int = QR_OK
