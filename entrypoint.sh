#!/usr/bin/env bash
set -e

CONFIG=/data/config.json

if [ ! -f "$CONFIG" ]; then
    cat >"$CONFIG" <<'EOF'
{
  "download_dir": "/music",
  "need_scrape_dir": "/music/need_scrape",
  "priority_order": ["netease","qq","luoshe"],
  "platforms": {
    "netease": {"cookie": "", "user_id": ""},
    "qq":      {"cookie": "", "user_id": ""},
    "kugou":   {"cookie": ""},
    "kuwo":    {"cookie": ""},
    "gis":     {"cookie": ""}
  },
  "scheduler": {"auto_sync": false, "auto_download": false, "time": "02:00"},
  "domain": "_",
  "ssl_email": "",
  "timezone": "Asia/Shanghai",
  "app_port": 13570
}
EOF
fi

APP_PORT=$(jq -r '.app_port // 13570' "$CONFIG")

TZ=$(jq -r '.timezone // "Asia/Shanghai"' "$CONFIG")
ln -sf "/usr/share/zoneinfo/$TZ" /etc/localtime
echo "$TZ" > /etc/timezone

python - <<'PY'
from app.db.models import Base, engine
Base.metadata.create_all(engine)
print("DB initialized")
PY

exec uvicorn app.main:app --host 0.0.0.0 --port "$APP_PORT"