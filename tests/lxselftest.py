"""洛雪音源沙箱自测：加载、取链、越权拦截、超时熔断。

在容器内运行：docker exec music-sync python /tmp/lxselftest.py
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/app")

import aiohttp

BASE = "http://127.0.0.1:3100"

_ok: list = []
_bad: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (_ok if cond else _bad).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra else ""))


async def post(path: str, payload: dict) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        async with s.post(BASE + path, json=payload) as r:
            return await r.json(content_type=None)


GOOD = """
/* @name 测试音源 @version 1.0.0 @author selftest */
const { EVENT_NAMES, on, send } = globalThis.lx
on(EVENT_NAMES.request, ({ action, source, info }) => {
  if (action === 'musicUrl') {
    return 'https://example.com/audio.mp3?q=' + info.type + '&s=' + source + '&id=' + info.musicInfo.id
  }
})
send(EVENT_NAMES.inited, { status: true, sources: { wy: { name: '网易云', qualitys: ['320k', '128k'] } } })
"""

THROWS = """
const { EVENT_NAMES, on, send } = globalThis.lx
on(EVENT_NAMES.request, () => { throw new Error('上游音源挂了') })
send(EVENT_NAMES.inited, { status: true, sources: { wy: { qualitys: ['320k'] } } })
"""

BAD_SYNTAX = "this is not javascript ((( "

NO_REQUIRE = """
const fs = require('fs')
const { EVENT_NAMES, on, send } = globalThis.lx
on(EVENT_NAMES.request, () => 'https://example.com/a.mp3')
send(EVENT_NAMES.inited, { status: true, sources: { wy: { qualitys: ['320k'] } } })
"""

LOOP = "while (true) {}"

NO_INIT = """
const { EVENT_NAMES, on } = globalThis.lx
on(EVENT_NAMES.request, () => 'https://example.com/a.mp3')
// 故意不发送 inited
"""


async def main() -> int:
    # 1. 正常脚本：检测
    d = await post("/check", {"script": GOOD})
    check("正常脚本检测通过", d.get("ok") is True, str(d.get("error") or ""))
    check("解析出脚本名", d.get("name") == "测试音源", str(d.get("name")))
    check("解析出版本号", d.get("version") == "1.0.0", str(d.get("version")))
    check("识别出平台 wy", d.get("platforms") == ["wy"], str(d.get("platforms")))
    check("识别出音质档位", d.get("qualityMap", {}).get("wy") == ["320k", "128k"],
          str(d.get("qualityMap")))

    # 2. 正常脚本：取链
    u = await post("/url", {"script": GOOD, "platform": "wy", "quality": "320k",
                            "musicInfo": {"id": 123, "name": "t", "singer": "a"}})
    check("取链成功", u.get("ok") is True and "example.com/audio.mp3" in str(u.get("url")),
          str(u.get("url") or u.get("error")))
    check("取链带上音质与平台", "q=320k" in str(u.get("url")) and "s=wy" in str(u.get("url")),
          str(u.get("url")))
    check("取链带上歌曲 id", "id=123" in str(u.get("url")))

    # 3. 脚本内部抛错 → 返回失败而不是崩
    e = await post("/url", {"script": THROWS, "platform": "wy", "quality": "320k", "musicInfo": {"id": 1}})
    check("脚本抛错能优雅返回", e.get("ok") is False and "上游音源挂了" in str(e.get("error")),
          str(e.get("error")))

    # 4. 语法错误
    s = await post("/check", {"script": BAD_SYNTAX})
    check("语法错误脚本被拒绝", s.get("ok") is False, str(s.get("error"))[:60])

    # 5. 沙箱禁止 require('fs')
    r = await post("/check", {"script": NO_REQUIRE})
    check("沙箱禁止 require('fs')", r.get("ok") is False and "禁止" in str(r.get("error")),
          str(r.get("error"))[:60])

    # 6. 死循环 → 加载超时
    l = await post("/check", {"script": LOOP})
    check("死循环脚本被超时中断", l.get("ok") is False and "超时" in str(l.get("error")),
          str(l.get("error"))[:60])

    # 7. 未发送 inited → 初始化超时
    n = await post("/check", {"script": NO_INIT})
    check("未初始化脚本被拒绝", n.get("ok") is False and "inited" in str(n.get("error")),
          str(n.get("error"))[:60])

    # 8. 空脚本
    z = await post("/check", {"script": "   "})
    check("空脚本被拒绝", z.get("ok") is False, str(z.get("error"))[:60])

    print(f"\n通过 {len(_ok)} 项，失败 {len(_bad)} 项")
    if _bad:
        print("失败项：" + "、".join(_bad))
    return 1 if _bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
