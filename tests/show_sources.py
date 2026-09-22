"""打印音源列表（人类可读）"""
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
print("  沙箱可用:", d["runner"])
for s in d["sources"]:
    print(f"  · {s['name']} v{s['version']} | 启用={s['enabled']} 可用={s['ok']} "
          f"| 平台={s['platforms']} 音质={s['qualities']} | {s['note']}")
