"""四大平台的歌曲搜索（内置），用于「跨平台音源回退」。

职责单一：**在目标平台上把这首歌找出来，拿到它在该平台的 id**。
取链仍交给用户导入的 LX 音源完成 —— 搜索与取链拆开，
对应 SPlayer-Next 里 PLATFORM_TO_PLUGIN_SOURCE 的那层映射：

    netease -> wy      qqmusic -> tx      kugou -> kg

它把「官方接口」与「插件」拆成两级；我们同理：
  * 搜索走内置接口 —— 不依赖音源是否实现 musicSearch
    （实测手头 8 个音源没有一个实现，所以这层必须自己做）
  * 取链走用户的音源 —— 覆盖面广，音源可随时更换

实测（2026-09-23，NAS 容器内）：tx / kg / kw / mg 四个平台均可用。
注意：这些都是第三方平台的公开接口，可能随时失效；
任何异常都只当作「该平台没搜到」，不影响回退链继续往下走。
"""
from __future__ import annotations

import json
import ssl
from typing import Any, Callable, Dict, List

import aiohttp

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# 部分平台 CDN 的证书与域名不匹配（如 mobilecdn.kugou.com），跳过校验
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

TIMEOUT = 12


def _h(referer: str = '') -> Dict[str, str]:
    h = {'User-Agent': _UA}
    if referer:
        h['Referer'] = referer
    return h


async def _get(session, url, params=None, headers=None) -> Any:
    async with session.get(url, params=params, headers=headers or _h(), ssl=_SSL) as r:
        return await r.text()


async def _get_json(session, url, params=None, headers=None) -> Any:
    async with session.get(url, params=params, headers=headers or _h(), ssl=_SSL) as r:
        return await r.json(content_type=None)


# ---------------------------------------------------------------- 各平台
async def _tx(session, keyword: str, limit: int) -> List[Dict[str, Any]]:
    """QQ音乐。注意三个必要点：comm 字段、search_type=7（单曲）、remoteplace。"""
    body = {
        'comm': {'ct': 19, 'cv': 1859, 'uin': '0'},
        'req': {
            'method': 'DoSearchForQQMusicDesktop',
            'module': 'music.search.SearchCgiService',
            'param': {'num_per_page': limit, 'page_num': 1, 'query': keyword,
                      'search_type': 7, 'remoteplace': 'txt.mqq.all'},
        },
    }
    async with session.post('https://u.y.qq.com/cgi-bin/musicu.fcg', json=body,
                            headers=_h('https://y.qq.com/'), ssl=_SSL) as r:
        d = await r.json(content_type=None)
    songs = (((d.get('req') or {}).get('data') or {}).get('body') or {}) \
        .get('song', {}).get('list') or []
    out: List[Dict[str, Any]] = []
    for x in songs:
        mid = x.get('mid') or (x.get('file') or {}).get('media_mid')
        if not mid:
            continue
        al = x.get('album') or {}
        out.append({
            'name': x.get('name') or x.get('title') or '',
            'singer': '/'.join(a.get('name', '') for a in (x.get('singer') or []) if a.get('name')),
            'songmid': str(mid),
            'id': str(mid),
            'albumName': al.get('name') or '',
            'albumId': str(al.get('id') or ''),
            'interval': x.get('interval'),
            'source': 'tx',
        })
    return out


async def _kg(session, keyword: str, limit: int) -> List[Dict[str, Any]]:
    """酷狗。必须用 http：mobilecdn 的 https 证书与域名不匹配。取链要用 hash。"""
    d = await _get_json(session, 'http://mobilecdn.kugou.com/api/v3/search/song',
                        params={'format': 'json', 'keyword': keyword, 'page': 1,
                                'pagesize': limit, 'showtype': 1})
    out: List[Dict[str, Any]] = []
    for x in ((d.get('data') or {}).get('info') or []):
        h = x.get('hash')
        if not h:
            continue
        out.append({
            'name': x.get('songname') or '',
            'singer': x.get('singername') or '',
            'songmid': str(h),
            'hash': str(h),
            'id': str(h),
            'albumName': x.get('album_name') or '',
            'albumId': str(x.get('album_id') or ''),
            'interval': x.get('duration'),
            'source': 'kg',
        })
    return out


async def _kw(session, keyword: str, limit: int) -> List[Dict[str, Any]]:
    """酷我。返回的是「单引号 JSON」，需要先把引号换成双引号再解析。"""
    t = await _get(session, 'http://search.kuwo.cn/r.s',
                   params={'all': keyword, 'ft': 'music', 'itemset': 'web_2013',
                           'client': 'kt', 'pn': 0, 'rn': limit,
                           'vipver': 'MUSIC_9.1.1.2_BCS2', 'encoding': 'utf8',
                           'rformat': 'json', 'ver': 'kwplayer_ar_9.2.2.1'})
    try:
        d = json.loads(t.replace("'", '"'))
    except ValueError:
        return []
    out: List[Dict[str, Any]] = []
    for x in (d.get('abslist') or []):
        rid = x.get('MUSICRID')
        if not rid:
            continue
        out.append({
            'name': str(x.get('SONGNAME') or '').replace('&nbsp;', ' '),
            'singer': str(x.get('ARTIST') or '').replace('&nbsp;', ' '),
            'songmid': str(rid),
            'id': str(rid),
            'albumName': str(x.get('ALBUM') or ''),
            'albumId': '',
            'interval': x.get('DURATION'),
            'source': 'kw',
        })
    return out


async def _mg(session, keyword: str, limit: int) -> List[Dict[str, Any]]:
    """咪咕。走 app.c.nf.migu.cn 的官方接口（m.music.migu.cn 会返回 HTML 反爬页）。"""
    sw = json.dumps({'song': 1, 'album': 0, 'singer': 0, 'tagSong': 0,
                     'mvSong': 0, 'bestShow': 0, 'songlist': 0},
                    separators=(',', ':'))
    d = await _get_json(session, 'https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do',
                        params={'ua': 'Android_migu', 'version': '5.0.1', 'text': keyword,
                                'pageNo': 1, 'pageSize': limit, 'searchSwitch': sw},
                        headers=_h('https://m.music.migu.cn/'))
    out: List[Dict[str, Any]] = []
    for x in ((d.get('songResultData') or {}).get('result') or []):
        cid = x.get('copyrightId') or x.get('id')
        if not cid:
            continue
        albums = x.get('albums') or []
        out.append({
            'name': x.get('name') or '',
            # 实测字段名是 singers（singerList 恒为 null）
            'singer': '/'.join(s.get('name', '') for s in (x.get('singers') or []) if s.get('name')),
            'songmid': str(cid),
            'id': str(cid),
            'copyrightId': str(cid),
            'albumName': (albums[0].get('name') if albums else '') or '',
            'albumId': '',
            'interval': x.get('length') or x.get('duration'),
            'source': 'mg',
        })
    return out


# 平台代号与 lx 音源一致：wy=网易云（走原生 id，不需要搜索）、tx/kg/kw/mg
_HANDLERS: Dict[str, Callable] = {'tx': _tx, 'kg': _kg, 'kw': _kw, 'mg': _mg}

# 供 UI/日志展示
PLATFORM_NAMES = {'wy': '网易云', 'tx': 'QQ音乐', 'kg': '酷狗', 'kw': '酷我', 'mg': '咪咕'}


async def search(platform: str, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
    """在指定平台搜索歌曲。任何异常都吞掉并返回空列表（回退链不应因此中断）。"""
    fn = _HANDLERS.get(platform)
    keyword = (keyword or '').strip()
    if not fn or not keyword:
        return []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as s:
            items = await fn(s, keyword, max(1, min(int(limit or 10), 30)))
    except Exception:  # noqa: BLE001  第三方接口，任何异常都只当「这个平台没搜到」
        return []
    for it in items:
        it.setdefault('source', platform)
    return items
