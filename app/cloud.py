"""网易云云盘：列表 / 配额 / 上传 / 匹配元数据 / 删除（全部经 ncm-api）。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict
from urllib.parse import unquote

import aiohttp

from app.ncm import NCM_BASE, NcmError


class UploadError(NcmError):
    """上传失败。

    retryable=True 表示「原样再传一次可能就过」（5xx / 429 / 连接被掐、超时），
    由上层统一做自动重试；retryable=False 是重传也没用的错（文件不存在、参数错）。
    继承 NcmError，原有 except NcmError 的调用方行为不变。
    """

    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = bool(retryable)


async def _get(path: str, cookie: str, timeout: int = 60, **params: Any) -> Dict[str, Any]:
    params.setdefault("timestamp", int(time.time() * 1000))
    params["cookie"] = cookie
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(f"{NCM_BASE}{path}", params=params) as r:
                data = await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        raise NcmError(f"云盘接口不可用：{type(e).__name__}") from e
    return data if isinstance(data, dict) else {}


async def list_songs(cookie: str, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    """云盘歌曲列表；返回 {data:[{simpleSong, fileSize, bitrate, ...}], count, size, maxSize}"""
    return await _get("/user/cloud", cookie, limit=max(1, int(limit)), offset=max(0, int(offset)))


async def quota(cookie: str) -> Dict[str, Any]:
    """云盘容量：已用 size / 上限 maxSize，歌曲数 count"""
    data = await _get("/user/cloud", cookie, limit=1, offset=0)
    return {
        "count": int(data.get("count") or 0),
        "size": int(data.get("size") or 0),
        "max_size": int(data.get("maxSize") or 0),
    }


async def upload(cookie: str, path: Path) -> Dict[str, Any]:
    """上传本地音频文件到云盘（大文件，超时放宽）

    HTTP 5xx / 429 / 408、响应不是 JSON（多半是网关错误页）、连接被掐或读超时，
    都抛 retryable=True 的 UploadError —— 这些是「再传一次就好」的抖动，
    由调用方（上传队列）统一重试。其余失败照常抛出/返回响应体。
    """
    path = Path(path)
    if not path.exists():
        raise UploadError("本地文件不存在")          # 文件没了，重传没意义
    params = {"cookie": cookie, "timestamp": int(time.time() * 1000)}
    timeout = aiohttp.ClientTimeout(total=3600, sock_connect=30, sock_read=600)
    with open(path, "rb") as fh:
        form = aiohttp.FormData()
        form.add_field("songFile", fh, filename=path.name,
                       content_type="application/octet-stream")
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{NCM_BASE}/cloud", params=params, data=form) as r:
                    status = int(r.status)
                    try:
                        data = await r.json(content_type=None)
                    except Exception:                # 非 JSON：多半是网关错误页
                        # 4xx 非 JSON = 请求被明确拒绝（重传没意义）；其余当抖动，可重试
                        retry = not (400 <= status < 500) or status in (408, 429)
                        raise UploadError(
                            f"上传失败：接口返回 HTTP {status}（响应不是 JSON）",
                            retryable=retry)
        except UploadError:
            raise
        except Exception as e:  # noqa: BLE001
            raise UploadError(f"上传请求失败：{type(e).__name__}", retryable=True) from e
    if not isinstance(data, dict):
        raise UploadError("上传返回异常", retryable=True)
    if status >= 400:
        # 响应体里通常已带网易云自己的 code；没有就补上 HTTP 状态，方便上层报错
        data = dict(data)
        data.setdefault("code", status)
        data.setdefault("msg", f"接口返回 HTTP {status}")
    return data


# 上传成功可能返回 200 或 201
UPLOAD_OK_CODES = (200, 201)


def upload_retry_kind(data: Dict[str, Any]) -> str:
    """这次的失败值不值得原样重传，返回 ""（不重试）/ "parse" / "server"

    * 409 —— 网易云偶发「音频解析失败」，与文件本身无关，实测原样重传就能过；
    * 5xx —— 网关 / 上游抖动（实测见过 504），隔一会儿再传通常就好。

    其余码（400 参数错、权限不足…）重传也是同样结果，直接报错。
    """
    code = int((data or {}).get("code") or 0)
    if code == 409:
        return "parse"
    if 500 <= code < 600:
        return "server"
    return ""


def uploaded_song_id(data: Dict[str, Any]) -> str:
    """取云盘歌曲 id（数字 id，用于 song/detail、cloud/match、cloud/del）

    注意：顶层 songId 是内容哈希，真正的云盘歌曲 id 在 privateCloud.simpleSong.id。
    """
    pc = data.get("privateCloud") or {}
    if isinstance(pc, dict):
        ss = pc.get("simpleSong") or pc.get("song") or {}
        if isinstance(ss, dict) and ss.get("id"):
            return str(ss["id"])
        for key in ("songId", "songid", "id"):
            v = pc.get(key)
            if v and str(v).isdigit():
                return str(v)
    for key in ("songId", "songid"):
        v = data.get(key)
        if v and str(v).isdigit():          # 只有纯数字才算歌曲 id
            return str(v)
    return ""


def upload_ok(data: Dict[str, Any]) -> bool:
    code = data.get("code")
    if code in UPLOAD_OK_CODES:
        return True
    return False


async def match(cookie: str, uid: str, sid: str, asid: str) -> Dict[str, Any]:
    """云盘歌曲匹配：把云盘条目(sid) 绑定到网易云正式曲目(asid)。

    绑定后这首歌就会使用正式曲目的元数据 / 封面 / 歌词 —— 也是「纠错」的手段：
    网易云自动匹配错了，就手动指定正确的歌曲 id 重新匹配。
    """
    return await _get("/cloud/match", cookie, uid=uid, sid=sid, asid=asid)


async def delete(cookie: str, sid: str) -> Dict[str, Any]:
    return await _get("/user/cloud/del", cookie, id=sid)


def _fmt_time(ts: int) -> str:
    """时间戳（秒）→ 'YYYY-MM-DD HH:MM:SS'，容器时区（TZ=Asia/Shanghai）"""
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (OSError, ValueError, OverflowError):
        return ""


def simplify(item: Dict[str, Any]) -> Dict[str, Any]:
    """把云盘列表项整理成界面需要的字段"""
    simple = item.get("simpleSong") or item.get("song") or {}
    artists = [a.get("name") for a in (simple.get("ar") or simple.get("artists") or []) if a.get("name")]
    al = simple.get("al") or simple.get("album") or {}
    pc = item.get("privateCloud") or {}
    # 云盘条目的封面只有两个来源：① 匹配到正式曲目（al.id 非 0）→ 官方封面；
    # ② 网易云自己存过封面（coverId 非 0）。都没有时它给的就是那张占位图。
    cover_id = str(pc.get("coverId") or pc.get("cover") or "0")
    al_id = int(al.get("id") or 0)
    # 有没有封面不能只看 al_id：下架歌没有专辑（al.id=0），但网易云照样给它存了
    # 封面（al.picUrl 有值）。三种来源任一命中就算有：正式曲目专辑 / 网易云自存封面 / 官方封面图。
    has_cover = bool(al_id) or cover_id not in ("0", "", "None") or bool(al.get("picUrl"))
    # 「匹配到正式曲目」不能只看 al_id：下架歌（如周杰伦那批、电影原声）官方**没有专辑信息**
    # （al.id=0），但 simpleSong 里已经有官方的歌名/歌手/封面 —— 网易云照样把它匹配到了那首
    # 下架歌，只是这首歌"下架"不能播。只看 al_id 会把它们误判成"没匹配"，进而：
    #   · 云盘索引把它们塞进「未匹配」的歌名集合，sid 精确匹配失效、退化成歌名猜；
    #   · 整理页/云盘页显示成"未匹配"。
    # 所以：有官方歌名 + 歌手 = 已经识别出是哪首歌 = 已匹配（无论有没有专辑）；
    # 即使 ar 异常（个别下架歌 ar=[None]），只要有官方封面（al.picUrl）也算已匹配。
    matched = bool(al_id) or bool(al.get("picUrl")) or bool(simple.get("name") and artists)
    # 云盘里这条的**原始文件名**（接口给的是 URL 编码）。页面上显示出来，
    # 用户就能拿「文件名里的 歌手-歌名」跟网易云识别出的标题/歌手对照，看有没有认错。
    fname = str(item.get("fileName") or "")
    if fname and "%" in fname:
        try:
            fname = unquote(fname)
        except Exception:  # noqa: BLE001
            pass
    # 上传（加入云盘）的时间：接口给的是毫秒，页面上精确到秒显示，
    # 方便核对「云盘里这个版本是什么时候传上去的」。
    add_time = int(item.get("addTime") or 0)
    add_time = add_time // 1000 if add_time > 10_000_000_000 else add_time
    return {
        "sid": str(simple.get("id") or item.get("songId") or ""),
        "title": str(simple.get("name") or item.get("songName") or "未知曲目"),
        "artist": " / ".join(artists) or str(item.get("artist") or "未知歌手"),
        "album": str(al.get("name") or item.get("album") or ""),
        "cover": str(al.get("picUrl") or ""),
        "has_cover": has_cover,
        "file_name": fname,
        # 云盘上这份文件的 md5（网易云存的就是**原文件字节**的 md5，实测与本地
        # `hashlib.md5(文件)` 完全一致）→ 用它可以判断「云盘那份和我这份是不是同一个
        # 文件」，比按歌名/条目 id 猜可靠得多。
        "md5": str(pc.get("md5") or "").lower(),
        "add_time": add_time,
        "add_time_str": _fmt_time(add_time),
        # 匹配到正式曲目时 sid 就是网易云歌曲 id（能直接和歌单里的 id 对上）；
        # 没匹配上时 sid 只是云盘条目自己的 id，只能靠标题/歌手认。
        "matched": matched,
        "al_id": al_id,
        # 已下架：网易云官方还保留这首歌的元数据（有歌名/歌手/封面），但版权没了、
        # 不能播放 —— 典型特征就是「匹配到了正式曲目、却没有专辑信息」（al.id=0）。
        # 页面据此打「已下架」角标，并可单独筛选。
        "delisted": bool(matched and not al_id),
        "match_type": str(pc.get("matchType") or ""),
        "duration": int((simple.get("dt") or 0) // 1000),
        "file_size": int(item.get("fileSize") or 0),
        "bitrate": int(item.get("bitrate") or 0),
    }
