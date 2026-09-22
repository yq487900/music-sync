#!/bin/bash
# 音源管理接口自测（添加/列表/启停/检测/删除）
B=${B:-http://127.0.0.1:13570}   # 换地址：B=http://你的NAS:13570 ./tests/source_api_test.sh
DIR=$(dirname "$0")

python3 "$DIR/mkpayload.py" > "$DIR/payload1.json"

echo "=== 1. 添加音源（粘贴脚本，应可用） ==="
curl -s -X POST "$B/api/sources" -H 'Content-Type: application/json' \
     --data-binary "@$DIR/payload1.json" | python3 -m json.tool

echo
echo "=== 2. 列表 ==="
curl -s "$B/api/sources" > "$DIR/sources.json"
python3 "$DIR/show_sources.py" "$DIR/sources.json"

SID=$(python3 -c "import json,sys;d=json.load(open('$DIR/sources.json'));print(d['sources'][0]['id'] if d['sources'] else '')")
echo "测试音源 id = $SID"

echo
echo "=== 3. 停用 / 启用 ==="
curl -s -X PATCH "$B/api/sources/$SID" -H 'Content-Type: application/json' -d '{"enabled":false}' \
  | python3 -c "import sys,json;print('  停用后 enabled =', json.load(sys.stdin)['source']['enabled'])"
curl -s -X PATCH "$B/api/sources/$SID" -H 'Content-Type: application/json' -d '{"enabled":true}' \
  | python3 -c "import sys,json;print('  启用后 enabled =', json.load(sys.stdin)['source']['enabled'])"

echo
echo "=== 4. 重新检测 ==="
curl -s -X POST "$B/api/sources/$SID/check" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('  可用:', d['source']['ok'], '|', d['source']['note'])"

echo
echo "=== 5. 越权脚本应被拒绝但能保存（标记为不可用） ==="
curl -s -X POST "$B/api/sources" -H 'Content-Type: application/json' \
     -d '{"name":"坏脚本","script":"const fs = require(\"fs\")"}' > "$DIR/bad.json"
python3 -c "import json;d=json.load(open('$DIR/bad.json'));s=d['source'];print('  已保存 id =',s['id'],'| 可用 =',s['ok'],'| 说明 =',s['note'])"

echo
echo "=== 6. 删除两个测试音源 ==="
curl -s -X DELETE "$B/api/sources/$SID"; echo
BADID=$(python3 -c "import json;print(json.load(open('$DIR/bad.json'))['source']['id'])")
curl -s -X DELETE "$B/api/sources/$BADID"; echo
curl -s "$B/api/sources" | python3 -c "import sys,json;print('  剩余音源数:', len(json.load(sys.stdin)['sources']))"

rm -f "$DIR/payload1.json" "$DIR/sources.json" "$DIR/bad.json"
