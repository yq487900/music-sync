from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import config as cfg_mod
from app.sync.netease import NeteaseSync
from app.db.models import SessionLocal, Track
from app.sources.luoshe import LuosheSource
from app.utils.hash import compute_hashes, get_duration
from pathlib import Path
import asyncio, os

scheduler = AsyncIOScheduler()

async def sync_all():
    cfg = cfg_mod.load()
    for platform, info in cfg.get("platforms", {}).items():
        cookie = info.get("cookie", "")
        if not cookie:
            continue
        if platform == "netease":
            user_id = info.get("user_id", "")
            if not user_id:
                continue
            ns = NeteaseSync(cookie)
            playlists = await ns.get_user_playlists(user_id)
            db = SessionLocal()
            for pl in playlists:
                pl_id = str(pl.get("id"))
                existing = db.query(Track).filter_by(platform="netease", platform_track_id=pl_id).first()
                if not existing:
                    tracks = await ns.get_playlist_tracks(pl_id)
                    for t in tracks:
                        tr = Track(
                            platform="netease",
                            platform_track_id=str(t.get("id")),
                            title=t.get("name"),
                            artist=",".join([a.get("name") for a in t.get("artists", [])]),
                            duration=t.get("duration",0)/1000,
                            metadata=t
                        )
                        db.add(tr)
            db.commit()
            db.close()

async def download_pending():
    cfg = cfg_mod.load()
    db = SessionLocal()
    tracks = db.query(Track).filter_by(downloaded=False).all()
    source = LuosheSource()
    for tr in tracks:
        dest_dir = Path(cfg["download_dir"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        file_path = dest_dir / f"{tr.artist} - {tr.title}.mp3"
        # check local duplicate
        if file_path.exists():
            md5, sha = compute_hashes(file_path)
            dur = get_duration(file_path)
            if abs(dur - tr.duration) < 2:
                tr.downloaded = True
                db.commit()
                continue
        results = await source.search(tr.title, tr.artist)
        if results:
            ok = await source.download(results[0], str(file_path))
            if ok:
                tr.downloaded = True
                db.commit()
    db.close()

def start_scheduler():
    cfg = cfg_mod.load()
    sch = cfg.get("scheduler", {})
    if sch.get("auto_sync"):
        h,m = map(int, sch.get("time","02:00").split(":"))
        scheduler.add_job(sync_all, "cron", hour=h, minute=m, id="auto_sync")
    if sch.get("auto_download"):
        h,m = map(int, sch.get("time","02:00").split(":"))
        scheduler.add_job(download_pending, "cron", hour=h, minute=m+5, id="auto_download")
    scheduler.start()