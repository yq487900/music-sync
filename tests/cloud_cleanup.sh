#!/bin/bash
# 云盘测试残留清理：找出测试歌名的云盘条目并删除，核对数量
B=${B:-http://127.0.0.1:13570}   # 换地址：B=http://你的NAS:13570 ./tests/cloud_cleanup.sh
j() { python3 -c "import sys,json;d=json.load(sys.stdin);$1"; }

echo "=== 当前云盘数量 ==="
BEFORE=$(curl -s -m 30 "$B/api/cloud/quota" | j "print(d.get('count'))")
echo "  count = $BEFORE"

echo
echo "=== 扫描测试条目 ==="
FOUND=""
for page in 1 2 3 4 5 6; do
  OUT=$(curl -s -m 40 "$B/api/cloud?page=$page&size=100" | python3 -c "
import sys, json
d = json.load(sys.stdin)
hits = [x for x in d.get('items', [])
        if x['title'] in ('silence', 'tone', 'hermes-cloud-selftest', 'Hermes云盘自测')
        or 'selftest' in x['title'].lower()]
for h in hits:
    print(h['sid'] + '\t' + h['title'] + '\t' + h['artist'])
print('PAGES', d.get('pages', 1))
")
  echo "$OUT" | grep -v '^PAGES' | while IFS=$'\t' read -r sid title artist; do
    [ -n "$sid" ] && echo "  命中: $title / $artist / sid=$sid"
  done
  FOUND="$FOUND $(echo "$OUT" | grep -v '^PAGES' | cut -f1 | tr '\n' ' ')"
  PAGES=$(echo "$OUT" | grep '^PAGES' | awk '{print $2}')
  [ "$PAGES" = "1" ] && break
done

echo
echo "=== 删除命中条目 ==="
for sid in $FOUND; do
  [ -z "$sid" ] && continue
  echo -n "  删除 $sid -> "
  curl -s -X DELETE "$B/api/cloud/$sid" | j "print(d)"
  sleep 1
done

sleep 3
echo
echo "=== 清理后数量 ==="
AFTER=$(curl -s -m 30 "$B/api/cloud/quota" | j "print(d.get('count'))")
echo "  清理前 = $BEFORE ，清理后 = $AFTER"
