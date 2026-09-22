"""生成添加音源的请求体（避免在 shell 里处理多行脚本）"""
import json

SCRIPT = """/* @name 测试音源 @version 1.0.0 @author selftest */
const { EVENT_NAMES, on, send } = globalThis.lx
on(EVENT_NAMES.request, ({ action, info }) => {
  if (action === 'musicUrl') return 'https://example.com/a.mp3'
  return null
})
send(EVENT_NAMES.inited, { status: true, sources: { wy: { qualitys: ['320k', '128k'] } } })
"""

print(json.dumps({"name": "测试音源", "script": SCRIPT}, ensure_ascii=False))
