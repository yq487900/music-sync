#!/usr/bin/env node
/**
 * 洛雪(LX)兼容音源脚本沙箱
 *
 * 提供 /check、/url、/search 三个接口，在一个受限 vm 上下文里加载用户导入的音源脚本，
 * 用于在网易云拿不到可用直链时（未登录试听、无版权、会员曲目）从第三方音源取链。
 *
 * 参考实现思路来自 miyin (github.com/qwex888/miyin) 的音源运行时。
 * 沙箱内禁止 fs / child_process / 网络模块，脚本只能通过 lx.request 发请求。
 */
'use strict'

// 音源脚本是第三方代码，常有异步错误（例如 init 请求失败后 throw）——
// 未处理的 Promise rejection / 未捕获异常会让 node 直接退出（exit status 1），
// 把整个沙箱搞崩，连带后面所有音源检测全部失败。这里兜底：只记日志，绝不让进程退出。
process.on('unhandledRejection', (e) => {
  console.error('[lx-runner] unhandledRejection:', (e && e.message) || e)
})
process.on('uncaughtException', (e) => {
  console.error('[lx-runner] uncaughtException:', (e && e.message) || e)
})

const http = require('node:http')
const { createCipheriv, createHash, publicEncrypt, randomBytes, constants } = require('node:crypto')
const { inflate, deflate } = require('node:zlib')
const { Script, createContext } = require('node:vm')
const { URL } = require('node:url')
const { inspect, promisify } = require('node:util')

const inflateAsync = promisify(inflate)
const deflateAsync = promisify(deflate)

const PORT = Number(process.env.LX_PORT || 3100)
const HOST = process.env.LX_HOST || '127.0.0.1'
const LOAD_TIMEOUT_MS = Number(process.env.LX_LOAD_TIMEOUT_MS || 8000)
const INIT_WAIT_MS = Number(process.env.LX_INIT_WAIT_MS || 12000)
const CALL_TIMEOUT_MS = Number(process.env.LX_CALL_TIMEOUT_MS || 25000)
const CACHE_TTL_MS = 5 * 60 * 1000
const MAX_TIMERS = 32
const MAX_FAILS = 3
const BREAK_MS = 60_000
const MAX_SCRIPT_BYTES = 2 * 1024 * 1024

const EVENT_NAMES = { request: 'request', inited: 'inited', updateAlert: 'updateAlert' }
const BLOCKED_REQUIRE = new Set([
  'fs', 'node:fs', 'fs/promises', 'node:fs/promises',
  'child_process', 'node:child_process', 'worker_threads', 'node:worker_threads',
  'os', 'node:os', 'net', 'node:net', 'dgram', 'node:dgram',
  'cluster', 'node:cluster', 'vm', 'node:vm', 'process', 'node:process',
])
const ALLOWED_REQUIRE = new Set(['crypto', 'node:crypto', 'buffer', 'node:buffer', 'url', 'node:url'])

const cache = new Map()      // hash -> {handle, loadedAt}
const breaker = new Map()    // hash -> {fails, openUntil}

function keyOf(script) {
  return createHash('sha256').update(script).digest('hex').slice(0, 16)
}

function withTimeout(promise, ms, label) {
  let timer
  return new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}超时(${ms}ms)`)), ms)
    promise.then(
      (v) => { clearTimeout(timer); resolve(v) },
      (e) => { clearTimeout(timer); reject(e) },
    )
  })
}

/** 解析脚本头部 @name / @version / @author 等元数据（兼容一行写多个 @ 标签） */
function parseHeader(code) {
  const head = String(code).slice(0, 4000)
  const get = (k) => {
    const m = head.match(new RegExp('@' + k + '\\s+([^\\r\\n*@]+)', 'i'))
    return m ? m[1].trim() : ''
  }
  return {
    name: get('name'),
    description: get('description'),
    version: get('version'),
    author: get('author'),
    homepage: get('homepage'),
  }
}

/** 沙箱内可用的网络请求：只允许 http/https，带超时与响应体上限 */
function lxRequest(url, options, callback) {
  let opts = options
  let cb = callback
  if (typeof options === 'function') { cb = options; opts = {} }
  opts = opts || {}

  const p = new Promise((resolve, reject) => {
    let settled = false
    const done = (err, resp) => {
      if (settled) return
      settled = true
      if (err) reject(err); else resolve(resp)
    }
    try {
      const u = new URL(url)
      if (u.protocol !== 'http:' && u.protocol !== 'https:') throw new Error('仅允许 http/https')
      const lib = u.protocol === 'https:' ? require('node:https') : require('node:http')
      const method = (opts.method || 'GET').toUpperCase()
      const headers = Object.assign({}, opts.headers || {})
      let payload
      if (opts.body != null) {
        payload = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body)
        if (!headers['Content-Type'] && !headers['content-type']) headers['Content-Type'] = 'application/json'
        headers['Content-Length'] = Buffer.byteLength(payload)
      }
      const req = lib.request({
        protocol: u.protocol, hostname: u.hostname,
        port: u.port || (u.protocol === 'http:' ? 80 : 443),
        path: u.pathname + u.search, method, headers, timeout: 15000,
      }, (res) => {
        const chunks = []
        let size = 0
        res.on('data', (c) => {
          size += c.length
          if (size > 8 * 1024 * 1024) { req.destroy(); done(new Error('音源响应体过大')); return }
          chunks.push(c)
        })
        res.on('end', () => {
          const raw = Buffer.concat(chunks).toString('utf8')
          let body = raw
          const ct = String(res.headers['content-type'] || '')
          if (ct.includes('json') || raw.trim().startsWith('{') || raw.trim().startsWith('[')) {
            try { body = JSON.parse(raw) } catch { body = raw }
          }
          done(null, { statusCode: res.statusCode || 0, body, headers: res.headers })
        })
        res.on('error', done)
      })
      req.on('error', done)
      req.on('timeout', () => { req.destroy(); done(new Error('request timeout')) })
      if (payload) req.write(payload)
      req.end()
    } catch (e) { done(e) }
  })

  if (cb) {
    p.then((r) => cb(null, r), (e) => cb(e))
    p.catch(() => {})
  }
  return p
}

function lxUtils() {
  return {
    buffer: {
      from: (...a) => Buffer.from(...a),
      bufToString: (buf, format) =>
        typeof buf === 'string' ? Buffer.from(buf, 'binary').toString(format || 'utf8')
          : Buffer.from(buf).toString(format || 'utf8'),
    },
    crypto: {
      aesEncrypt(buffer, mode, key, iv) {
        const c = createCipheriv(mode, key, iv)
        return Buffer.concat([c.update(buffer), c.final()])
      },
      rsaEncrypt(buffer, key) {
        const buf = Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer)
        const padded = Buffer.concat([Buffer.alloc(Math.max(0, 128 - buf.length)), buf])
        return publicEncrypt({ key, padding: constants.RSA_NO_PADDING }, padded)
      },
      randomBytes: (n) => randomBytes(n),
      md5: (s) => createHash('md5').update(String(s)).digest('hex'),
    },
    zlib: { inflate: (b) => inflateAsync(b), deflate: (d) => deflateAsync(d) },
  }
}

function assertClosed(hash) {
  const c = breaker.get(hash)
  if (c && c.openUntil > Date.now()) {
    throw new Error(`音源熔断中，请稍后再试（${Math.ceil((c.openUntil - Date.now()) / 1000)}s）`)
  }
}

function recordFailure(hash) {
  const cur = breaker.get(hash) || { fails: 0, openUntil: 0 }
  cur.fails += 1
  if (cur.fails >= MAX_FAILS) { cur.openUntil = Date.now() + BREAK_MS; cur.fails = 0 }
  breaker.set(hash, cur)
}

/** 在 vm 沙箱里加载脚本，返回句柄 */
async function load(script) {
  const hash = keyOf(script)
  assertClosed(hash)
  const hit = cache.get(hash)
  if (hit && Date.now() - hit.loadedAt < CACHE_TTL_MS) return hit.handle

  if (Buffer.byteLength(script, 'utf8') > MAX_SCRIPT_BYTES) throw new Error('音源脚本过大，拒绝加载')

  const handlers = []
  const logs = []
  const alerts = []
  let platforms = []
  let qualityMap = {}
  let disposed = false
  let didInit = false
  let resolveInit = null
  const initPromise = new Promise((r) => { resolveInit = r })
  const timers = new Set()

  const safeSetTimeout = (fn, ms, ...args) => {
    const id = setTimeout(() => { timers.delete(id); if (!disposed) fn(...args) }, ms)
    if (timers.size >= MAX_TIMERS) { clearTimeout(id); throw new Error('沙箱定时器数量超限') }
    timers.add(id)
    return id
  }
  const safeSetInterval = (fn, ms, ...args) => {
    const id = setInterval(() => { if (!disposed) fn(...args) }, ms)
    if (timers.size >= MAX_TIMERS) { clearInterval(id); throw new Error('沙箱定时器数量超限') }
    timers.add(id)
    return id
  }
  const safeClear = (id) => { clearTimeout(id); clearInterval(id); timers.delete(id) }
  const pushLog = (level, args) => {
    let text
    try { text = args.map((a) => typeof a === 'string' ? a : inspect(a, { depth: 3, breakLength: 120, maxStringLength: 2000 })).join(' ') }
    catch { text = String(args) }
    if (logs.length < 200) logs.push(`[${level}] ${text}`.slice(0, 800))
  }

  const scriptInfo = parseHeader(script)
  const lx = {
    EVENT_NAMES,
    env: 'desktop',
    version: '2.0.0',
    currentScriptInfo: scriptInfo,
    utils: lxUtils(),
    request: lxRequest,
    on(name, fn) {
      if (name === EVENT_NAMES.request && typeof fn === 'function') handlers.push(fn)
      return Promise.resolve()
    },
    send(name, payload) {
      if (name === EVENT_NAMES.inited) {
        didInit = true
        let sources = (payload && (payload.sources || (payload.init && payload.init.sources))) || {}
        sources = sources.sources || sources
        platforms = []
        qualityMap = {}
        for (const [k, v] of Object.entries(sources || {})) {
          if (!v || typeof v !== 'object') continue
          platforms.push(k)
          qualityMap[k] = v.qualitys || ['128k']
        }
        resolveInit && resolveInit()
      }
      if (name === EVENT_NAMES.updateAlert) {
        const text = String((payload && (payload.log || payload.message)) || payload || '').trim()
        if (text) alerts.push(text.slice(0, 300))
      }
      return Promise.resolve()
    },
  }

  const sandbox = {
    console: {
      log: (...a) => pushLog('log', a),
      info: (...a) => pushLog('info', a),
      warn: (...a) => pushLog('warn', a),
      error: (...a) => pushLog('error', a),
      debug: (...a) => pushLog('debug', a),
      group: () => {}, groupEnd: () => {},
    },
    setTimeout: safeSetTimeout,
    setInterval: safeSetInterval,
    clearTimeout: safeClear,
    clearInterval: safeClear,
    Buffer,
    URL,
    TextEncoder,
    TextDecoder,
    module: { exports: {} },
    exports: {},
    require: (id) => {
      if (ALLOWED_REQUIRE.has(id)) return require(id)
      throw new Error(`沙箱禁止 require('${id}')`)
    },
  }
  sandbox.globalThis = sandbox
  sandbox.global = sandbox
  sandbox.lx = lx

  try {
    const ctx = createContext(sandbox, { name: 'lx-source' })
    new Script(script, { filename: 'source.js' }).runInContext(ctx, { timeout: LOAD_TIMEOUT_MS })
  } catch (e) {
    for (const t of timers) safeClear(t)
    recordFailure(hash)
    const msg = String((e && e.message) || e)
    throw new Error(msg.includes('timed out') ? '音源脚本初始化超时（疑似死循环）' : msg)
  }

  if (!didInit) {
    const timedOut = await Promise.race([
      initPromise.then(() => false),
      new Promise((r) => setTimeout(() => r(true), INIT_WAIT_MS)),
    ])
    if (timedOut && !didInit) {
      for (const t of timers) safeClear(t)
      recordFailure(hash)
      throw new Error(`音源初始化超时（${INIT_WAIT_MS}ms 内未发送 inited）`)
    }
  }
  if (!handlers.length) {
    for (const t of timers) safeClear(t)
    recordFailure(hash)
    throw new Error('音源未注册取链处理函数')
  }
  if (!platforms.length) {
    platforms = ['wy', 'kw', 'kg', 'tx', 'mg']
    qualityMap = Object.fromEntries(platforms.map((p) => [p, ['128k', '320k']]))
  }

  const handle = {
    hash, platforms, qualityMap, scriptInfo, logs, alerts,
    async getMusicUrl(platform, musicInfo, quality) {
      if (disposed) throw new Error('音源已释放')
      assertClosed(hash)
      const info = { type: quality, musicInfo }
      const ret = await withTimeout(
        Promise.resolve().then(() => handlers[0]({ action: 'musicUrl', source: platform, info })),
        CALL_TIMEOUT_MS, '取链',
      )
      let url
      if (typeof ret === 'string' && /^https?:/.test(ret)) url = ret
      else if (ret && ret.url) url = String(ret.url)
      if (!url) throw new Error('音源未返回播放地址')
      breaker.delete(hash)
      return url
    },
    async searchMusic(platform, keyword, page, limit) {
      if (disposed) throw new Error('音源已释放')
      assertClosed(hash)
      const info = { keyword, page: page || 1, limit: limit || 30 }
      const ret = await withTimeout(
        Promise.resolve().then(() => handlers[0]({ action: 'musicSearch', source: platform, info })),
        CALL_TIMEOUT_MS, '搜索',
      )
      let list = []
      if (Array.isArray(ret)) list = ret
      else if (ret && Array.isArray(ret.list)) list = ret.list
      else if (ret && Array.isArray(ret.data)) list = ret.data
      return list.filter((x) => x && typeof x === 'object')
    },
    dispose() {
      disposed = true
      handlers.length = 0
      for (const t of timers) safeClear(t)
      timers.clear()
      cache.delete(hash)
    },
  }
  cache.set(hash, { handle, loadedAt: Date.now() })
  // 只保留最近 8 个脚本，避免内存堆积
  if (cache.size > 8) {
    const oldest = [...cache.entries()].sort((a, b) => a[1].loadedAt - b[1].loadedAt)[0]
    try { oldest[1].handle.dispose() } catch { /* ignore */ }
  }
  return handle
}

// ------------------------------------------------------------------ HTTP
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', (c) => {
      size += c.length
      if (size > 4 * 1024 * 1024) { reject(new Error('请求体过大')); req.destroy(); return }
      chunks.push(c)
    })
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')) }
      catch (e) { reject(new Error('请求体不是合法 JSON')) }
    })
    req.on('error', reject)
  })
}

function send(res, code, obj) {
  const body = JSON.stringify(obj)
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) })
  res.end(body)
}

const server = http.createServer(async (req, res) => {
  const path = (req.url || '').split('?')[0]
  if (req.method === 'GET' && path === '/health') return send(res, 200, { ok: true, cached: cache.size })
  if (req.method !== 'POST') return send(res, 405, { ok: false, error: '只支持 POST' })
  let body
  try { body = await readBody(req) } catch (e) { return send(res, 400, { ok: false, error: String(e.message || e) }) }
  const script = String(body.script || '')
  if (!script.trim()) return send(res, 400, { ok: false, error: '缺少音源脚本内容' })

  try {
    if (path === '/check') {
      const h = await load(script)
      return send(res, 200, {
        ok: true, name: h.scriptInfo.name, version: h.scriptInfo.version,
        author: h.scriptInfo.author, description: h.scriptInfo.description,
        platforms: h.platforms, qualityMap: h.qualityMap,
        alerts: h.alerts, logs: h.logs.slice(-30),
      })
    }
    if (path === '/search') {
      const h = await load(script)
      const platform = String(body.platform || 'wy')
      const keyword = String(body.keyword || '').trim()
      if (!keyword) return send(res, 200, { ok: false, error: '缺少搜索关键词' })
      try {
        const list = await h.searchMusic(platform, keyword, body.page, body.limit)
        return send(res, 200, { ok: true, list, count: list.length, logs: h.logs.slice(-10) })
      } catch (e) {
        return send(res, 200, { ok: false, error: String((e && e.message) || e), logs: h.logs.slice(-10) })
      }
    }
    if (path === '/url') {
      const h = await load(script)
      const platform = String(body.platform || 'wy')
      const quality = String(body.quality || '320k')
      const musicInfo = body.musicInfo && typeof body.musicInfo === 'object' ? body.musicInfo : {}
      try {
        const url = await h.getMusicUrl(platform, musicInfo, quality)
        return send(res, 200, { ok: true, url, logs: h.logs.slice(-15) })
      } catch (e) {
        return send(res, 200, { ok: false, error: String((e && e.message) || e), logs: h.logs.slice(-15) })
      }
    }
    return send(res, 404, { ok: false, error: '未知接口' })
  } catch (e) {
    return send(res, 200, { ok: false, error: String((e && e.message) || e) })
  }
})

server.listen(PORT, HOST, () => {
  console.log(`[lx-runner] listening on http://${HOST}:${PORT}`)
})
