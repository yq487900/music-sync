#!/bin/bash
# 云盘上传全链路自测：造小音频 → 入库 → 上传 → 核对 → 删除 → 核对数量
B=${B:-http://127.0.0.1:13570}   # 换地址：B=http://你的NAS:13570 ./tests/cloud_api_test.sh
# 在容器里跑脚本：有 docker 权限就直接 docker exec；没有（比如以普通用户跑）就用
# ROOTRUN 指向一个能执行 docker 的转发脚本，例如 ROOTRUN=/path/rootrun.sh
run_in_ct() {  # 用法：run_in_ct /tmp/xxx.sh [参数...]
  if [ -n "${ROOTRUN:-}" ]; then "${ROOTRUN}" "$@"; else docker exec music-sync bash "$@"; fi
}


j() { python3 -c "import sys,json;d=json.load(sys.stdin);$1"; }

cat > /tmp/ct.py <<'PY'
import hashlib, shutil, subprocess, sys
from pathlib import Path
sys.path.insert(0, "/app")
from app.db.models import SessionLocal, Track

action = sys.argv[1] if len(sys.argv) > 1 else ""
db = SessionLocal()

if action == "prep":
    d = Path("/data/tmp/cloudtest"); d.mkdir(parents=True, exist_ok=True)
    mp3 = d / "hermes-cloud-selftest.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", "3", "-q:a", "9", str(mp3)], capture_output=True, check=True)
    md5 = hashlib.md5(mp3.read_bytes()).hexdigest()
    row = db.query(Track).filter_by(platform_track_id="selftest-cloud").first()
    if row is None:
        row = Track(platform="netease", platform_track_id="selftest-cloud")
        db.add(row)
    row.title, row.artist, row.album = "Hermes云盘自测", "自测", "自测专辑"
    row.duration, row.status, row.selected, row.downloaded = 3, "ok", False, True
    row.file_path, row.ext = str(mp3), "mp3"
    row.md5, row.size = md5, mp3.stat().st_size
    row.cloud_state, row.cloud_sid, row.cloud_error = "", "", ""
    db.commit()
    print(row.id)
elif action == "info":
    row = db.query(Track).filter_by(platform_track_id="selftest-cloud").first()
    if row is None:
        print("GONE")
    else:
        print(f"{row.id}|{row.cloud_sid or ''}|{row.cloud_state or ''}|{row.cloud_error or '无'}")
elif action == "clean":
    for r in db.query(Track).filter(Track.platform_track_id == "selftest-cloud").all():
        db.delete(r)
    db.commit()
    shutil.rmtree("/data/tmp/cloudtest", ignore_errors=True)
    print("cleaned")
db.close()
PY

# rootrun 只转发第一个参数，所以每个动作单独一个脚本
for act in prep info clean; do
  cat > /tmp/ct_$act.sh <<EOS
#!/bin/bash
docker cp /tmp/ct.py music-sync:/tmp/ct.py >/dev/null 2>&1
docker exec music-sync python /tmp/ct.py $act
EOS
done

echo "=== 上传前云盘歌曲数 ==="
BEFORE=$(curl -s -m 30 "$B/api/cloud/quota" | j "print(d.get('count'))")
echo "  count = $BEFORE"

echo
echo "=== 1. 造 3 秒测试音频并入库 ==="
TID=$(run_in_ct /tmp/ct_prep.sh | tail -1 | tr -d '[:space:]')
echo "  本地 track_id = $TID"
if [ -z "$TID" ]; then echo "  ❌ 准备失败，终止"; run_in_ct /tmp/ct_clean.sh; exit 1; fi

echo
echo "=== 2. 入队上传 ==="
curl -s -X POST "$B/api/cloud/upload/$TID" | j "print('  ', d)"

echo
echo "=== 3. 等上传结束 ==="
for i in $(seq 1 30); do
  ST=$(curl -s -m 20 "$B/api/queue" | TID=$TID python3 -c "
import os,sys,json
tid=int(os.environ['TID'])
d=json.load(sys.stdin)
t=[x for x in d['upload']['tasks'] if x['track_id']==tid]
print(t[0]['state'] if t else 'gone')")
  if [ "$ST" = "done" ] || [ "$ST" = "failed" ]; then break; fi
  sleep 2
done
curl -s -m 20 "$B/api/queue" | TID=$TID python3 -c "
import os,sys,json
tid=int(os.environ['TID'])
d=json.load(sys.stdin)
t=[x for x in d['upload']['tasks'] if x['track_id']==tid]
print('  任务结果:', (t[0]['state'] + ' / ' + (t[0]['error'] or '无错误')) if t else '已清理')"

echo
echo "=== 4. 核对 DB 与云盘 ==="
run_in_ct /tmp/ct_info.sh | tail -1 | python3 -c "
import sys
line = sys.stdin.read().strip()
parts = line.split('|')
if len(parts) >= 4:
    print(f'  DB: cloud_sid={parts[1] or \"(空)\"} state={parts[2] or \"(空)\"} error={parts[3]}')
else:
    print('  DB:', line)"
curl -s -m 30 "$B/api/cloud/quota" | j "print('  配额:', {k:d.get(k) for k in ('count','uploaded_by_us','waiting')})"

echo
echo "=== 5. 删除测试歌曲并清理本地 ==="
CSID=$(run_in_ct /tmp/ct_info.sh | tail -1 | cut -d'|' -f2 | tr -d '[:space:]')
if [ -n "$CSID" ]; then
  curl -s -X DELETE "$B/api/cloud/$CSID" | j "print('  删除结果:', d)"
else
  echo "  没有 cloud_sid（上传未成功），无需删除"
fi
sleep 3
run_in_ct /tmp/ct_clean.sh | tail -1

echo
echo "=== 6. 最终数量（应与上传前一致） ==="
AFTER=$(curl -s -m 30 "$B/api/cloud/quota" | j "print(d.get('count'))")
echo "  上传前 = $BEFORE ，清理后 = $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then echo "  ✅ 云盘已恢复原状"; else echo "  ⚠️ 差 $((AFTER-BEFORE)) 首，需人工检查"; fi
