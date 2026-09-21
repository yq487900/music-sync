from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import asyncio, json, os
from app import config as cfg_mod
import app.config as config_mod
from app.db.models import SessionLocal, Playlist, Track
from app.sources.luoshe import LuosheSource
from app.auth.netease_qr import NeteaseQR
from app.auth.qq_qr import QQQR
from app.scheduler import start_scheduler, sync_all, download_pending

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    start_scheduler()
    yield
    # shutdown
    from app.scheduler import scheduler
    scheduler.shutdown()

app = FastAPI(title="MusicSync", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/tmp/music-sync/app/static"), name="static")
templates = Jinja2Templates(directory="/tmp/music-sync/app/templates")

# ---------- Pages ----------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cfg = cfg_mod.load()
    return templates.TemplateResponse("index.html", {"request": request, "config": cfg})

@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    cfg = cfg_mod.load()
    return templates.TemplateResponse("config.html", {"request": request, "config": cfg})

@app.post("/config")
async def save_config(download_dir: str = Form(...), source_order: str = Form(""), auto_sync: bool = Form(False), auto_download: bool = Form(False), sync_time: str = Form("02:00")):
    cfg = cfg_mod.load()
    cfg["download_dir"] = download_dir
    if source_order:
        cfg["priority_order"] = [s.strip() for s in source_order.split(",") if s.strip()]
    cfg["scheduler"]["auto_sync"] = auto_sync
    cfg["scheduler"]["auto_download"] = auto_download
    cfg["scheduler"]["time"] = sync_time
    config_mod.save(cfg)
    return JSONResponse({"status":"ok"})

@app.get("/playlists")
async def playlists():
    db = SessionLocal()
    pls = db.query(Playlist).all()
    db.close()
    return {"playlists": [{"platform":p.platform,"id":p.playlist_id,"title":p.title} for p in pls]}

# ---------- QR Login ----------
@app.get("/login/netease", response_class=HTMLResponse)
async def netease_login_page(request: Request):
    return templates.TemplateResponse("login_netease.html", {"request": request})

@app.post("/api/login/netease/qr")
async def netease_qr():
    qr = NeteaseQR()
    key = await qr.get_qr_key()
    qr_url = await qr.create_qr(key)
    await qr.close()
    return {"key": key, "qr_url": qr_url}

@app.post("/api/login/netease/poll")
async def netease_poll(key: str = Form(...)):
    qr = NeteaseQR()
    cookie = await qr.login_wait(timeout=120)
    await qr.close()
    if cookie:
        cfg = cfg_mod.load()
        cfg["platforms"]["netease"]["cookie"] = cookie
        config_mod.save(cfg)
        return {"status":"success","cookie":cookie}
    return {"status":"pending"}

@app.get("/login/qq", response_class=HTMLResponse)
async def qq_login_page(request: Request):
    return templates.TemplateResponse("login_qq.html", {"request": request})

# ---------- Manual Sync ----------
@app.post("/api/sync/netease")
async def api_sync_netease(background_tasks: BackgroundTasks):
    background_tasks.add_task(sync_all)
    return {"status":"started"}

# ---------- Manual Download ----------
@app.post("/api/download/pending")
async def api_download_pending(background_tasks: BackgroundTasks):
    background_tasks.add_task(download_pending)
    return {"status":"started"}

# ---------- API ----------
@app.get("/api/config")
def api_config():
    return cfg_mod.load()