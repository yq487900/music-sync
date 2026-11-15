"""内置网易云接口服务（ncm-api）的版本检查与热更新。

为什么需要这个模块：ncm-api 的版本原本固定在 Dockerfile 的 `ARG NCM_API_VERSION` 里，
上游接口一变动（例如 4.41.0 修掉 xeapi 公钥地址）就得改代码 + 重建镜像 + 部署，
对一个「只是想跟上上游」的小改动来说太重了。这里给两条更轻的路径：

  * **检查**：问 npm registry 要 `dist-tags.latest`，和容器内已装版本比；
    结果缓存在内存 + config.json（`ncm_api.*`），页面/定时任务都读缓存，不反复打网络。
  * **更新**：容器内 `npm install` 覆盖 `/opt/ncm-api`，重打 entrypoint 那个
    `checkVersion` 补丁，然后**只重启 ncm-api 这一个 supervisord 进程**
    （不重建容器、不影响 Web 界面和队列）。

热更新的版本号记在 `config.json` 的 `ncm_api.installed_version`：
`entrypoint.sh` 启动时会拿它和镜像内置版本比对，不一致就自动装回来 ——
所以容器重建 / 镜像升级都不会把热更新弄丢。

安全设计：
  * 安装前把当前包目录备份到 `/data/ncm-pkg-backup`；
  * 装完必须通过健康检查（/login/status 有响应）才算成功，否则**自动回滚**并重启回旧版本；
  * `upgrade()` 用模块级锁，避免并发点两次按钮把 node_modules 拆坏。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

NCM_PKG = "@neteasecloudmusicapienhanced/api"
NCM_ROOT = Path("/opt/ncm-api")
NCM_PKG_DIR = NCM_ROOT / "node_modules" / "@neteasecloudmusicapienhanced"
NCM_PKG_JSON = NCM_PKG_DIR / "api" / "package.json"
NCM_APP_JS = NCM_PKG_DIR / "api" / "app.js"
BACKUP_DIR = Path("/data/ncm-pkg-backup")          # 回滚用的包目录副本
SUPERVISOR_SOCK = "unix:///tmp/supervisor.sock"    # 与 supervisord.conf 一致
NCM_HOST = "http://127.0.0.1:3000"

# 优先国内镜像（快），失败退官方源 —— 两者都可能被墙/限流，所以都试一遍
REGISTRIES = ("https://registry.npmmirror.com", "https://registry.npmjs.org")

DEFAULT_CFG: Dict[str, Any] = {
    "auto_update": False,       # 发现新版本后自动装（默认关，交给人决定）
    "check_hours": 24,          # 自动检查间隔（小时）
    "installed_version": "",    # 热更新后的版本；空=跟随镜像内置版本
    "last_check": "",
    "latest": "",
    "latest_published": "",
    "last_error": "",
}

# 运行期状态（进程序内存，页面轮询用）
STATE: Dict[str, Any] = {
    "checking": False,
    "upgrading": False,
    "stage": "",
    "log": [],          # 最近若干行进度
    "last_result": {},  # 上次 update 的结果
}


# ---------------------------------------------------------------- 版本号
def _ver_tuple(v: Any) -> tuple:
    """'4.41.1' → (4, 41, 1)；解析不出来就当 0"""
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def installed_version() -> str:
    """容器内实际装着的版本（读 package.json，最可信）"""
    try:
        data = json.loads(NCM_PKG_JSON.read_text(encoding="utf-8"))
        return str(data.get("version") or "")
    except Exception:   # noqa: BLE001
        return ""


def builtin_version() -> str:
    """镜像构建时固定的版本（Dockerfile 的 ARG 写到 /app/.ncm_builtin_version）"""
    try:
        return Path("/app/.ncm_builtin_version").read_text(encoding="utf-8").strip()
    except Exception:   # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 配置读写
def _cfg() -> Dict[str, Any]:
    """读/补 config.json 里的 ncm_api 段（独立读写，避免和 config 模块循环依赖）"""
    import app.config as cfgmod
    cfg = cfgmod.load()
    out = dict(DEFAULT_CFG)
    cur = cfg.get("ncm_api")
    if isinstance(cur, dict):
        out.update({k: v for k, v in cur.items() if v is not None})
    return out


def _save_cfg(**kw: Any) -> None:
    import app.config as cfgmod
    cfg = cfgmod.load()
    cur = cfg.get("ncm_api")
    if not isinstance(cur, dict):
        cur = dict(DEFAULT_CFG)
    cur.update(kw)
    cfg["ncm_api"] = cur
    cfgmod.save(cfg)


# ---------------------------------------------------------------- 查最新版
async def _fetch_one(session: aiohttp.ClientSession, registry: str) -> Dict[str, Any]:
    url = f"{registry}/{NCM_PKG.replace('/', '%2F')}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        data = await r.json(content_type=None)
    tags = data.get("dist-tags") or {}
    latest = str(tags.get("latest") or "")
    if not latest:
        raise RuntimeError("返回里没有 dist-tags.latest")
    return {"version": latest, "registry": registry,
            "published": str((data.get("time") or {}).get(latest) or "")}


async def fetch_latest() -> Dict[str, Any]:
    """问 registry 要最新版本；两个源都失败则返回 error"""
    errors: List[str] = []
    for reg in REGISTRIES:
        try:
            async with aiohttp.ClientSession() as s:
                got = await _fetch_one(s, reg)
            got["error"] = ""
            return got
        except Exception as e:  # noqa: BLE001
            errors.append(f"{reg.split('//')[-1]}: {type(e).__name__} {e}")
    return {"version": "", "registry": "", "published": "",
            "error": "；".join(errors) or "取不到版本信息"}


async def check(force: bool = False) -> Dict[str, Any]:
    """检查是否有新版本；结果写进 config.json（顺带缓存给页面/定时任务读）"""
    if STATE["checking"]:
        return await status()
    cfg = _cfg()
    cur = installed_version()
    if not force and cfg.get("last_check"):
        # 非强制检查：一个检查周期内复用上次结果，避免每次打开配置页都打网络
        try:
            hours = max(1, int(cfg.get("check_hours") or 24))
        except (TypeError, ValueError):
            hours = 24
        try:
            last = time.mktime(time.strptime(str(cfg["last_check"]), "%Y-%m-%d %H:%M:%S"))
            if time.time() - last < hours * 3600 and cfg.get("latest"):
                return await status()
        except (ValueError, OverflowError):
            pass

    STATE["checking"] = True
    try:
        got = await fetch_latest()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if got.get("error"):
            _save_cfg(last_check=now, last_error=str(got["error"])[:300])
            STATE["last_result"] = {"ok": False, "error": got["error"]}
        else:
            _save_cfg(last_check=now, latest=got["version"],
                      latest_published=got.get("published") or "",
                      last_error="")
            STATE["last_result"] = {"ok": True, "latest": got["version"]}
    finally:
        STATE["checking"] = False
    return await status()


async def status() -> Dict[str, Any]:
    """给页面用的完整状态（不发网络请求）"""
    cfg = _cfg()
    cur = installed_version()
    latest = str(cfg.get("latest") or "")
    has_update = bool(latest) and _ver_tuple(latest) > _ver_tuple(cur)
    return {
        "current": cur,
        "builtin": builtin_version(),
        "latest": latest,
        "latest_published": cfg.get("latest_published") or "",
        "has_update": has_update,
        "checked_at": cfg.get("last_check") or "",
        "auto_update": bool(cfg.get("auto_update")),
        "check_hours": int(cfg.get("check_hours") or 24),
        "last_error": cfg.get("last_error") or "",
        "checking": STATE["checking"],
        "upgrading": STATE["upgrading"],
        "stage": STATE["stage"],
        "log": STATE["log"][-12:],
        "last_result": STATE["last_result"],
    }


# ---------------------------------------------------------------- 热更新
def _log(line: str) -> None:
    STATE["log"].append(f"{time.strftime('%H:%M:%S')} {line}")
    STATE["log"] = STATE["log"][-40:]
    print(f"[ncm-update] {line}", flush=True)


def _patch_check_version() -> None:
    """重打 entrypoint 那个补丁：关掉启动时的 npm 版本检查，否则不监听端口"""
    try:
        if not NCM_APP_JS.exists():
            return
        text = NCM_APP_JS.read_text(encoding="utf-8")
        if "checkVersion: true" in text:
            NCM_APP_JS.write_text(text.replace("checkVersion: true", "checkVersion: false"),
                                  encoding="utf-8")
            _log("已关闭新版本里启动时的 npm 版本检查")
    except Exception as e:  # noqa: BLE001
        _log(f"补丁打失败（不影响功能）：{type(e).__name__}")


def _restart_ncm() -> bool:
    """只重启 ncm-api 进程（不重建容器、不动 Web 服务与队列）"""
    for args in (["supervisorctl", "-s", SUPERVISOR_SOCK, "restart", "ncm-api"],
                 ["supervisorctl", "restart", "ncm-api"]):
        try:
            p = subprocess.run(args, capture_output=True, timeout=90)
            out = (p.stdout or b"").decode(errors="ignore").strip()
            if p.returncode == 0:
                _log(f"ncm-api 已重启：{out[:120]}")
                return True
            _log(f"重启返回 {p.returncode}：{(p.stderr or b'').decode(errors='ignore')[:120]}")
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            _log(f"重启异常：{type(e).__name__}: {e}")
    return False


def _wait_healthy(timeout: int = 150) -> bool:
    """等 ncm-api 真正能应答（端口起来但进程假活也算失败，所以直接打业务接口）"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{NCM_HOST}/login/status", timeout=8) as r:
                if r.status < 500:
                    return True
        except Exception:   # noqa: BLE001
            pass
        time.sleep(3)
    return False


def _npm_install(version: str, timeout: int = 600) -> tuple:
    """容器内装指定版本；返回 (ok, 输出摘要)"""
    last = ""
    for reg in REGISTRIES:
        cmd = ["npm", "install", f"{NCM_PKG}@{version}", "--omit=dev",
               "--no-audit", "--no-fund", "--prefix", str(NCM_ROOT), "--registry", reg]
        _log(f"npm install @{version} ← {reg.split('//')[-1]}")
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"npm 超时（{timeout}s）"
            _log(last)
            continue
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            _log(last)
            continue
        out = ((p.stdout or b"") + (p.stderr or b"")).decode(errors="ignore").strip()
        if p.returncode == 0:
            return True, out[-400:]
        last = out[-400:] or f"npm 返回 {p.returncode}"
        _log(f"失败：{last[:200]}")
    return False, last


def _rollback(reason: str) -> None:
    """把备份的包目录换回去并重启，尽量恢复可用状态"""
    _log(f"回滚：{reason}")
    try:
        if BACKUP_DIR.exists():
            if NCM_PKG_DIR.exists():
                shutil.rmtree(NCM_PKG_DIR, ignore_errors=True)
            NCM_PKG_DIR.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(BACKUP_DIR, NCM_PKG_DIR)
            _log("已恢复旧版本文件")
    except Exception as e:  # noqa: BLE001
        _log(f"回滚文件失败：{type(e).__name__}: {e}")
    _patch_check_version()
    _restart_ncm()
    _wait_healthy(90)


def upgrade_sync(target: str = "") -> Dict[str, Any]:
    """同步执行一次热更新（调用方负责丢到线程里）。返回 {ok, from, to, log, error}"""
    if STATE["upgrading"]:
        return {"ok": False, "error": "已有更新在进行中"}
    cur = installed_version()
    target = (target or "").strip()
    if not target:
        cfg = _cfg()
        target = str(cfg.get("latest") or "")
    if not target:
        return {"ok": False, "error": "还没检查到最新版本，请先点「检查更新」"}
    if _ver_tuple(target) == _ver_tuple(cur):
        return {"ok": False, "error": f"当前已经是 {cur}"}

    STATE["upgrading"] = True
    STATE["log"] = []
    STATE["stage"] = "备份当前版本"
    t0 = time.time()
    try:
        # 1) 备份（只备份这一家的包目录，node_modules 其它依赖不动）
        try:
            if BACKUP_DIR.exists():
                shutil.rmtree(BACKUP_DIR, ignore_errors=True)
            BACKUP_DIR.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(NCM_PKG_DIR, BACKUP_DIR)
            _log(f"已备份当前版本 {cur}")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"备份失败，为安全起见不继续：{type(e).__name__}: {e}"}

        # 2) 安装
        STATE["stage"] = f"安装 {target}"
        ok, out = _npm_install(target)
        if not ok:
            STATE["stage"] = "回滚"
            _rollback("安装失败")
            return {"ok": False, "error": f"安装 {target} 失败：{out[-200:]}", "from": cur}

        got = installed_version()
        if _ver_tuple(got) != _ver_tuple(target):
            STATE["stage"] = "回滚"
            _rollback(f"装完版本不符（期望 {target}，实际 {got or '未知'}）")
            return {"ok": False, "from": cur,
                    "error": f"安装后版本不符：期望 {target}，实际 {got or '读不到'}"}

        # 3) 重打补丁 + 重启 + 健康检查
        STATE["stage"] = "重打补丁并重启"
        _patch_check_version()
        if not _restart_ncm():
            STATE["stage"] = "回滚"
            _rollback("ncm-api 重启失败")
            return {"ok": False, "from": cur, "error": "ncm-api 重启失败"}

        STATE["stage"] = "等待服务就绪"
        if not _wait_healthy():
            STATE["stage"] = "回滚"
            _rollback("新版本启动后接口无响应")
            return {"ok": False, "from": cur, "error": f"{target} 启动后接口无响应，已回滚"}

        # 4) 成功：把版本记进配置（容器重建时 entrypoint 会按它恢复）
        _save_cfg(installed_version=target, last_error="")
        STATE["stage"] = "完成"
        _log(f"更新完成 {cur} → {target}（{time.time() - t0:.0f}s）")
        return {"ok": True, "from": cur, "to": target,
                "seconds": round(time.time() - t0, 1), "log": STATE["log"][-12:]}
    finally:
        STATE["upgrading"] = False
        STATE["stage"] = ""


async def upgrade(target: str = "") -> Dict[str, Any]:
    """异步包装：npm install / 重启都是阻塞操作，丢到线程里跑"""
    return await asyncio.to_thread(upgrade_sync, target)


# ---------------------------------------------------------------- 定时任务
async def auto_tick() -> Dict[str, Any]:
    """定时任务入口：检查（必要时）→ 若开了自动更新且有新版则升级"""
    cfg = _cfg()
    st = await check(force=False)
    if STATE["upgrading"]:
        return {"skipped": True, "reason": "正在更新中"}
    if not (st.get("has_update") and st.get("auto_update")):
        return {"checked": True, "has_update": st.get("has_update"), "auto": st.get("auto_update")}
    # 队列忙的时候别动 ncm-api：重启会让正在下载的歌失败
    try:
        from app import runner
        snap = runner.snapshot() or {}
        busy = any((snap.get(k) or {}).get("busy") for k in ("download", "upload"))
    except Exception:   # noqa: BLE001
        busy = False
    if busy:
        _log("队列正忙，本轮跳过自动更新，稍后再试")
        return {"skipped": True, "reason": "队列正忙"}
    res = await upgrade(str(st.get("latest") or ""))
    print(f"[ncm-update] 自动更新结果：{res}", flush=True)
    return res
