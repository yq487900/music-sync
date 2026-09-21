import json, os
from pathlib import Path
from typing import Dict, Any

CONFIG_PATH = Path("/data/config.json")
DEFAULT = {
    "download_dir": "/music",
    "need_scrape_dir": "/music/need_scrape",
    "sources": {
        "netease": {"enabled": True, "priority": 10},
        "qq": {"enabled": True, "priority": 20},
        "luoshe": {"enabled": True, "priority": 30}
    },
    "priority_order": ["netease","qq","luoshe"],
    "platforms": {
        "netease": {"cookie": "", "user_id": ""},
        "qq": {"cookie": "", "user_id": ""},
        "kugou": {"cookie": ""},
        "kuwo": {"cookie": ""},
        "gis": {"cookie": ""}
    },
    "scheduler": {"auto_sync": False, "auto_download": False, "time": "02:00"}
}

def load() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = {**DEFAULT, **data}
    else:
        cfg = DEFAULT
    # ensure dirs
    Path(cfg["download_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["need_scrape_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg

def save(cfg: Dict[str, Any]):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load()