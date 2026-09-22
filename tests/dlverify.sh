#!/bin/bash
cd "$(dirname "$0")/.."          # 脚本所在仓库根目录
cat > /tmp/dlverify.py <<'PY'
"""登录状态下下载 2 首验证（写临时目录，不动音乐库）"""
import asyncio, shutil, sys
from pathlib import Path
sys.path.insert(0, "/app")

import app.config as cfgmod
from app.db.models import SessionLocal, Track
from app.runner import download_all

LIB = Path("/data/tmp/verifylib")
shutil.rmtree(LIB, ignore_errors=True)

cfg = cfgmod.load()
cookie = ((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or ""
print(f"登录 Cookie 长度: {len(cookie)}  音源数: {len(cfg.get('music_sources') or [])}")
cfg["download_dir"] = str(LIB)
cfg["limits"]["max_per_run"] = 2
cfg["limits"]["download_concurrency"] = 1

db = SessionLocal()
before = db.query(Track).filter(Track.status == "failed").count()
db.close()

async def main():
    s = await download_all(cfg, lambda st, d, dn=None, tt=None: print(f"    {st} / {d}"))
    print()
    print(f"成功 {s['ok']} | 失败 {s['failed']} | 音质 {s['levels']}")
    for f in s["failures"]:
        print("   失败:", f.get("artist"), "-", f.get("title"), "=>", f.get("reason"))
    print()
    if LIB.exists():
        for p in sorted(LIB.rglob("*")):
            rel = p.relative_to(LIB)
            print(("  📄 " if p.is_file() else "  📁 ") + str(rel) + (f"  ({p.stat().st_size} 字节)" if p.is_file() else ""))
    audio = sorted(LIB.rglob("*.mp3")) + sorted(LIB.rglob("*.flac")) + sorted(LIB.rglob("*.m4a"))
    if audio:
        from app.downloader import read_duration
        f = audio[0]
        print()
        print("抽查:", f.name, "| 实际时长", round(read_duration(f), 1), "秒")
        if f.suffix == ".mp3":
            from mutagen.id3 import ID3
            t = ID3(str(f))
            print("  标题:", t.get("TIT2"), "| 歌手:", t.get("TPE1"), "| 专辑:", t.get("TALB"))
            print("  封面帧:", len(t.getall("APIC")), "| 歌词帧:", len(t.getall("USLT")))

asyncio.run(main())

# 还原这 2 首的状态，避免测试数据影响后续统计
db = SessionLocal()
for tr in db.query(Track).filter(Track.status == "ok").limit(2).all():
    if tr.file_path and str(tr.file_path).startswith(str(LIB)):
        tr.status = "new"; tr.file_path = ""; tr.ext = ""; tr.size = 0; tr.md5 = ""
        tr.level = ""; tr.br = 0; tr.downloaded = False; tr.fail_count = 0; tr.last_error = ""
db.commit()
db.close()
shutil.rmtree(LIB, ignore_errors=True)
print()
print("已清理测试文件并还原曲目状态")
PY
docker cp /tmp/dlverify.py music-sync:/tmp/dlverify.py >/dev/null 2>&1
docker exec music-sync python /tmp/dlverify.py
