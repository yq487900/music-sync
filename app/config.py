import json
from pathlib import Path
from typing import Any, Dict

CONFIG_PATH = Path("/data/config.json")

# 音质优先级（高 → 低）
QUALITY_CHAIN = ["jymaster", "hires", "lossless", "exhigh", "standard"]

DEFAULT: Dict[str, Any] = {
    "download_dir": "/music/download",
    "library_dir": "/music/musics",
    "trash_dir": "/music/_trash",
    "platforms": {
        "netease": {"cookie": "", "user_id": ""},
    },
    "scheduler": {"auto_sync": False, "auto_download": False, "time": "02:00"},
    # 要同步的歌单：[{"id": 123, "name": "可选自定义名"}]
    "playlists": [],
    # 音乐库整理方式：album=按专辑 | artist=按歌手 | playlist=按歌单 | flat=平铺
    # auto_archive：下载/搜集来的歌，元数据已齐全（歌名/歌手/专辑/封面/歌词 + 网易云 id）
    # → 自动移进「整理后」曲库，不用再手动刮削一次
    "library": {"layout": "album", "naming": "", "auto_archive": True},
    "quality": {"chain": list(QUALITY_CHAIN), "upgrade_existing": True},
    "lyrics": {"lrc": True, "embed": True},
    "nfo": True,
    "limits": {
        "download_concurrency": 3,
        "api_delay": 0.35,
        "max_per_run": 0,        # 每轮最多下载数，0=不限
        "fail_backoff": 3,       # 连续失败多少次后进入退避
        "backoff_hours": 24,     # 退避时长
        # 跨平台回退：网易云音源拿不到直链时，去 QQ音乐/酷狗/酷我/咪咕 搜同名歌
        # 再让音源取链（搜索用内置接口，不依赖音源是否支持 musicSearch）
        "source_fallback": True,
    },
    # 第三方音源（洛雪兼容 JS 脚本）
    "music_sources": [],
    # 网易云云盘。calibrate_mode = 上传完成后对**本地文件**的校准力度：
    #   off  一个字节都不动（云盘那边照样匹配正式曲目，只是本地不碰）
    #   fill 只补空：本地缺的才补，已有的一律不覆盖（默认）
    #   full 按网易云官方信息逐项纠正（会覆盖手改过的值）
    # calibrate 是早期布尔开关，保留只为读老配置（True→full / False→off）
    # delete_local_after_upload：上传云盘成功后，自动把本地文件（连同封面/歌词/NFO）移进回收站
    "cloud": {"auto_upload": False, "calibrate": True, "calibrate_mode": "fill",
              "delete_local_after_upload": False},
    # 歌单监控：on=开关；mode=new 从现在开始监控 / full 全量扫描补齐后再监控
    "monitor": {"on": False, "mode": "new", "since": 0, "batch": 20, "interval": 2, "token": "",
                "last_run": "", "last_result": {}},
    # 曲库整理
    "organize": {"batch": 20},      # 一键批量刮削每轮处理的文件数
}

# 早期版本用过的字段，已废弃；读到就丢掉，避免和新字段冲突
LEGACY_KEYS = ("sources", "need_scrape_dir", "priority_order", "domain", "ssl_email", "app_port")

# 已下线平台的配置键（QQ 音乐等），加载时清掉
DROPPED_PLATFORMS = ("qq", "kugou", "kuwo", "gis")


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深合并：新增的配置项在老配置文件里缺失时用默认值补齐"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            data = {}
    cfg = _merge(DEFAULT, data if isinstance(data, dict) else {})
    for key in LEGACY_KEYS:
        cfg.pop(key, None)
    plats = cfg.get("platforms")
    if not isinstance(plats, dict):
        plats = {}
    for key in DROPPED_PLATFORMS:
        plats.pop(key, None)
    plats.setdefault("netease", {"cookie": "", "user_id": ""})
    cfg["platforms"] = plats
    if not isinstance(cfg.get("music_sources"), list):
        cfg["music_sources"] = []
    if not isinstance(cfg.get("playlists"), list):
        cfg["playlists"] = []
    if not isinstance(cfg.get("cloud"), dict):
        cfg["cloud"] = {"auto_upload": False, "calibrate": True, "calibrate_mode": "fill"}
    cfg["cloud"].setdefault("auto_upload", False)
    cfg["cloud"].setdefault("calibrate", True)
    cfg["cloud"].setdefault("delete_local_after_upload", False)
    # 老配置只写了布尔 calibrate（没有 calibrate_mode）→ 按它原来的值换算成档位，
    # 不能默认值盖掉：显式关过校准的人（calibrate=false）得保持关着。
    # 注意两点：① 要读**文件里的原值**（_merge 已用默认值补上了 calibrate_mode）；
    #          ② 全新安装（没有配置文件）不该走这里 —— 让默认值 fill 生效。
    saved_cloud = data.get("cloud") if isinstance(data, dict) else None
    if isinstance(saved_cloud, dict) and not str(saved_cloud.get("calibrate_mode") or ""):
        cfg["cloud"]["calibrate_mode"] = "full" if bool(saved_cloud.get("calibrate", True)) else "off"
    if not isinstance(cfg.get("monitor"), dict):
        cfg["monitor"] = {"on": False, "mode": "new", "since": 0, "batch": 20}
        cfg["monitor"].setdefault("interval", 2)
    cfg["monitor"].setdefault("on", False)
    cfg["monitor"].setdefault("mode", "new")
    cfg["monitor"].setdefault("since", 0)
    cfg["monitor"].setdefault("batch", 20)
    cfg["monitor"].setdefault("interval", 2)
    cfg["monitor"].setdefault("token", "")
    # 旧版 download_dir 是 /music 根目录 → 迁移到 /music/download 子目录
    if str(cfg.get("download_dir") or "").rstrip("/") == "/music":
        cfg["download_dir"] = "/music/download"
    for key in ("download_dir", "library_dir", "trash_dir"):
        try:
            Path(cfg[key]).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return cfg


def save(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def quality_chain(cfg: Dict[str, Any]) -> list:
    chain = [str(x) for x in ((cfg.get("quality") or {}).get("chain") or []) if str(x)]
    return chain or list(QUALITY_CHAIN)


def playlist_ids(cfg: Dict[str, Any]) -> list:
    out = []
    for p in cfg.get("playlists") or []:
        try:
            out.append(int(p["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


config = load()
