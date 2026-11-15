"""下载一致性校验：把「下载到的文件」与「网易云实际播放版本」逐项比对。

这是**下载与整理之间的强制关口**：任一比对项不通过，整首歌判不通过，
文件删除、不落库、不归档、不上传。

五项比对（见 docs/一致性校验.md）：

    duration    时长      本地音频时长  vs 网易云曲目时长（dt）
    artist      演唱者    本地内嵌标签  vs 网易云 ar 歌手列表
    album       所属专辑  本地内嵌标签  vs 网易云 al.name
    bgm         音乐 bgm  本地音频  vs 官方播放流   （梅尔谱对齐相关 → 编曲/伴奏是否同一版本）
    voiceprint  歌手声纹  本地音频  vs 官方播放流   （MFCC 帧相似度 + 基频分布 → 是否同一个人唱）

设计约束：
  * 本模块**不依赖数据库 / 全局配置**，入参只有「文件路径 + 基准元数据 + ncm 客户端 +
    校验配置」，因此可以被 CLI、单元测试、批量复核脚本独立调用。
  * 音频解码统一走 ffmpeg（容器内已装），特征计算用 numpy；两者缺失时音频项降级为 skip
    并在报告里写明原因，不会因为环境问题误杀歌曲。
  * 「声纹」是**轻量声学近似**（MFCC 倒谱 + 基频轨迹），不是深度学习声纹模型。
    不上模型是刻意的（镜像体积 + 每首数秒推理），需要更严格时可在这里替换成 embedding 比对。
"""
from __future__ import annotations

import asyncio
import difflib
import math
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:                       # numpy 只用于音频特征；元数据比对不需要它
    import numpy as np
except Exception:          # noqa: BLE001  pragma: no cover
    np = None

# ---------------------------------------------------------------- 默认参数
DEFAULT_VERIFY: Dict[str, Any] = {
    "enabled": True,
    # 要检查的项；空列表 = 只做元数据三项
    "items": ["duration", "artist", "album", "bgm", "voiceprint"],
    # 时长容差（秒）。同一条录音不同码率/容器会有几十 ms 误差，网易云 dt 本身也有 1s 级偏差，
    # 取 2 秒；试听片段（差几十秒）照样会被拦下。
    "duration_tolerance": 2.0,
    # 专辑名相似度下限（0~1，difflib 比率）
    "album_threshold": 0.82,
    # bgm：两侧梅尔谱（0.5s 网格 × 26 带）对齐后的平均相关系数下限
    #   实测（2026-10-26 曲库标定）：同一首歌 0.944~1.000；不同歌 0.527~0.572；
    #   Live 版 vs 录音室 0.569 → 取 0.80，离两侧都留有安全余量
    "bgm_threshold": 0.80,
    # 声纹：MFCC 帧级余弦相似度中位数下限
    #   实测：同一首歌 0.728~1.000（0.728 那首是曲库里的旧母带，音色略有差异）；
    #   不同歌 -0.005~0.159；Live vs 录音室 0.497 → 取 0.62
    "voiceprint_threshold": 0.62,
    # 基频中位数相对差上限（同一录音 < 2%；换调/变速的版本会明显超出）
    "pitch_tolerance": 0.06,
    # 拿不到官方播放流（已下架 / 无版权 / 只剩试听）时音频项怎么办：
    #   skip  标记「无法比对」但不算失败（默认；否则下架歌永远下载不了）
    #   fail  算作不通过
    "on_missing_reference": "skip",
    # 只校验第三方音源下载的曲目（官方直链下载时内容天然一致，跳过可省时间）
    "only_third_party": False,
    # 校验不通过时保留样本到 /data/verify_failed/（排查误报用），默认删掉
    "keep_failed": False,
}

# 音频分析参数（8kHz 单声道足够做人声/编曲比对，解码快）
SR = 8000
FRAME = 1024
HOP = 512
MEL_BANDS = 26
MEL_FMIN = 40.0
MEL_FMAX = 3800.0
GRID_SEC = 0.5            # 梅尔谱时间聚合粒度
REF_PROBE_SEC = 40.0      # 参考音频取多长（秒）——ffmpeg 对 URL 做 input seek，不整首下载
REF_POSITION = 0.35       # 从整首的多大比例处开始取比对片段（避开片头静音/淡入）
MAX_LAG_SEC = 4.0         # 对齐搜索窗口（±4 秒）

ITEM_LABELS = {
    "duration": "时长",
    "artist": "演唱者",
    "album": "所属专辑",
    "bgm": "音乐 bgm",
    "voiceprint": "歌手声纹",
}


# ---------------------------------------------------------------- 数据结构
@dataclass
class Item:
    """单个比对项的结果"""
    key: str
    state: str                     # pass / fail / skip
    detail: str = ""
    local: Any = None              # 本地实测值
    ref: Any = None                # 网易云基准值
    score: Optional[float] = None  # 相似度（音频项用）

    @property
    def label(self) -> str:
        return ITEM_LABELS.get(self.key, self.key)

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "state": self.state,
                "detail": self.detail, "local": self.local, "ref": self.ref,
                "score": None if self.score is None else round(float(self.score), 4)}


@dataclass
class Report:
    """整首歌的校验结论"""
    state: str                                    # passed / failed / skipped
    items: List[Item] = field(default_factory=list)
    reference: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.state == "passed"

    def failed_items(self) -> List[Item]:
        return [i for i in self.items if i.state == "fail"]

    def summary(self) -> str:
        """一句话结论（写进 last_error / 任务备注）"""
        if self.state == "skipped":
            return "一致性校验跳过" + (f"：{self.error}" if self.error else "")
        if self.passed:
            n = len([i for i in self.items if i.state == "pass"])
            s = len([i for i in self.items if i.state == "skip"])
            return f"一致性校验通过（{n}/{len(self.items)} 项）" + (f"，{s} 项无法比对" if s else "")
        bad = "、".join(f"{i.label}不一致" for i in self.failed_items()) or "未知项"
        first = self.failed_items()[0].detail if self.failed_items() else ""
        return f"一致性校验不通过：{bad}" + (f"（{first}）" if first else "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "passed": self.passed,
            "summary": self.summary(),
            "items": [i.to_dict() for i in self.items],
            "reference": self.reference,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


# ---------------------------------------------------------------- 配置
def verify_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从整份配置里取出校验配置并补齐默认值（允许只传子字典）"""
    raw = (cfg or {}).get("verify") if isinstance(cfg, dict) else None
    if raw is None:
        raw = cfg if isinstance(cfg, dict) and "enabled" in cfg else {}
    out = dict(DEFAULT_VERIFY)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if v is not None})
    if not isinstance(out.get("items"), (list, tuple)):
        out["items"] = list(DEFAULT_VERIFY["items"])
    for k in ("duration_tolerance", "album_threshold", "bgm_threshold",
              "voiceprint_threshold", "pitch_tolerance"):
        try:
            out[k] = float(out[k])
        except (TypeError, ValueError):
            out[k] = float(DEFAULT_VERIFY[k])
    return out


# ---------------------------------------------------------------- 文本归一化
_PUNCT = re.compile(r"[\s\u3000·・\-_—–~～!！?？,，.。、:：;；'\"“”‘’()（）\[\]【】<>《》/\\|&+*]+")
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b\.?", re.I)


def _norm(text: str) -> str:
    """比对用的归一化：小写、去标点空白、去 feat. 之类"""
    s = str(text or "").lower()
    s = _FEAT.sub(" ", s)
    s = _PUNCT.sub("", s)
    return s


def _split_artists(raw: Any) -> List[str]:
    """把 "周杰伦/费玉清"、"A & B"、"C feat. D" 拆成名单（保留原始写法）"""
    text = str(raw or "")
    if isinstance(raw, (list, tuple)):
        parts: List[str] = []
        for x in raw:
            parts += _split_artists(x)
        return parts
    text = _FEAT.sub("/", text)
    parts = re.split(r"\s*(?:/|、|;|；|&|\+|\||,|，)\s*", text)
    return [p.strip() for p in parts if p.strip()]


def artist_match(local: str, ref_artists: Sequence[str]) -> Tuple[bool, str]:
    """演唱者比对：本地标签里出现任一「主要歌手」即通过。

    规则（放宽为主，避免把正常的英文名/组合名判成错）：
      1) 归一化后完全相等 → 通过；
      2) 一方包含另一方（长度 ≥ 2）→ 通过（"周杰伦" vs "周杰伦 Jay Chou"）；
      3) 相似度 ≥ 0.75 → 通过（拼写小差异）；
    要求**至少命中一个**歌手：多歌手曲目本地只写了主唱不算错。
    """
    loc_parts = _split_artists(local)
    ref_parts = [p for p in (ref_artists or []) if str(p).strip()]
    if not loc_parts:
        return False, "本地文件没有演唱者标签"
    if not ref_parts:
        return False, "网易云没有歌手信息"
    loc_norm = [_norm(x) for x in loc_parts if _norm(x)]
    ref_norm = [x for x in (_norm(y) for y in ref_parts) if x]
    best, best_pair = 0.0, ("", "")
    for a in loc_norm:
        for b in ref_norm:
            if a == b:
                return True, ""
            if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
                return True, ""
            r = difflib.SequenceMatcher(None, a, b).ratio()
            if r > best:
                best, best_pair = r, (a, b)
    if best >= 0.75:
        return True, ""
    return False, f"本地「{'/'.join(loc_parts)}」≠ 网易云「{'/'.join(ref_parts)}」（最高相似 {best:.2f}）"


def album_match(local: str, ref: str, threshold: float) -> Tuple[bool, float]:
    a, b = _norm(local), _norm(ref)
    if not a or not b:
        return False, 0.0
    if a == b:
        return True, 1.0
    # 去掉 "(Deluxe Edition)"、"（豪华版）" 这类后缀再比一次
    strip = lambda s: re.sub(r"(deluxe|remaster(ed)?|edition|版|特别版|豪华版|expanded)$", "", s)  # noqa: E731
    a2, b2 = strip(a), strip(b)
    score = max(difflib.SequenceMatcher(None, a, b).ratio(),
                difflib.SequenceMatcher(None, a2, b2).ratio() if a2 and b2 else 0.0)
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        score = max(score, 0.9)
    return score >= threshold, score


# ---------------------------------------------------------------- 本地标签
def read_raw_tags(path: Path) -> Dict[str, str]:
    """读**文件自带**的标签（必须在写标签之前调用）。

    第三方音源给的音频通常带着上游的标题/歌手/专辑，这才是能和网易云比对的原始证据。
    """
    out = {"title": "", "artist": "", "album": ""}
    try:
        from mutagen import File as MutagenFile

        f = MutagenFile(str(path), easy=True)
        if f is None:
            return out
        for key in ("title", "artist", "album"):
            got = f.get(key)
            if got:
                out[key] = str(got[0]) if isinstance(got, (list, tuple)) else str(got)
            elif key == "artist" and f.get("albumartist"):
                got = f.get("albumartist")
                out["artist"] = str(got[0]) if isinstance(got, (list, tuple)) else str(got)
    except Exception:   # noqa: BLE001  读不到就当没有标签（对应项会被 skip 或按无值判）
        pass
    return out


def audio_duration(path: Path) -> float:
    """本地音频时长（秒）：tinytag 快，失败再用 ffprobe"""
    try:
        from tinytag import TinyTag
        d = float(TinyTag.get(str(path)).duration or 0)
        if d > 0:
            return d
    except Exception:   # noqa: BLE001
        pass
    return ffprobe_duration(path)


def ffprobe_duration(src: Any) -> float:
    """用 ffprobe 读时长；src 可以是路径或 bytes"""
    exe = shutil.which("ffprobe")
    if not exe:
        return 0.0
    try:
        if isinstance(src, (bytes, bytearray)):
            p = subprocess.run([exe, "-v", "quiet", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", "-i", "pipe:0"],
                               input=bytes(src), capture_output=True, timeout=60)
        else:
            p = subprocess.run([exe, "-v", "quiet", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", "-i", str(src)],
                               capture_output=True, timeout=60)
        return float((p.stdout or b"").decode().strip() or 0)
    except Exception:   # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------- 音频解码 / 特征
def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and np is not None)


def decode_pcm(src: Any, skip: float = 0.0, limit: Optional[float] = None,
               sr: int = SR) -> Optional["np.ndarray"]:
    """解码成 8kHz 单声道 float32；src 为路径或 bytes。

    skip/limit 用于「只取中间一段」：本地文件直接从磁盘 seek，参考片段本身已经很短。
    """
    exe = shutil.which("ffmpeg")
    if not exe or np is None:
        return None
    cmd = [exe, "-v", "quiet", "-nostdin"]
    if skip and skip > 0:
        cmd += ["-ss", f"{skip:.3f}"]
    if isinstance(src, (bytes, bytearray)):
        cmd += ["-i", "pipe:0"]
    else:
        cmd += ["-i", str(src)]
    if limit and limit > 0:
        cmd += ["-t", f"{limit:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        p = subprocess.run(cmd, input=(bytes(src) if isinstance(src, (bytes, bytearray)) else None),
                           capture_output=True, timeout=180)
    except Exception:   # noqa: BLE001
        return None
    if not p.stdout:
        return None
    arr = np.frombuffer(p.stdout, dtype="<f4")
    return arr.astype(np.float32, copy=False) if arr.size else None


def _frames(x: "np.ndarray", frame: int = FRAME, hop: int = HOP) -> "np.ndarray":
    if x is None or x.size < frame:
        return np.zeros((0, frame), dtype=np.float32)
    n = 1 + (x.size - frame) // hop
    idx = np.arange(frame, dtype=np.int64)[None, :] + hop * np.arange(n, dtype=np.int64)[:, None]
    return x[idx]


def _hz2mel(f: float) -> float:
    return 2595.0 * math.log10(1.0 + f / 700.0)


def _mel2hz(m: float) -> float:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


def _mel_bank(sr: int = SR, n_fft: int = FRAME, bands: int = MEL_BANDS) -> "np.ndarray":
    n_bins = n_fft // 2 + 1
    mels = np.linspace(_hz2mel(MEL_FMIN), _hz2mel(MEL_FMAX), bands + 2)
    hz = np.array([_mel2hz(m) for m in mels])
    bins = np.floor((n_fft + 1) * hz / sr).astype(int)
    fb = np.zeros((bands, n_bins), dtype=np.float32)
    for i in range(bands):
        left, center, right = int(bins[i]), int(bins[i + 1]), int(bins[i + 2])
        center = max(center, left + 1)
        right = max(right, center + 1)
        for k in range(left, min(center, n_bins)):
            fb[i, k] = (k - left) / max(1, center - left)
        for k in range(center, min(right, n_bins)):
            fb[i, k] = (right - k) / max(1, right - center)
    return fb


_MEL_FB = None


def mel_spec(x: "np.ndarray", sr: int = SR) -> "np.ndarray":
    """对数梅尔谱：每一帧一行，26 个梅尔带"""
    global _MEL_FB
    fr = _frames(x)
    if fr.shape[0] == 0:
        return np.zeros((0, MEL_BANDS), dtype=np.float32)
    if _MEL_FB is None:
        _MEL_FB = _mel_bank(sr)
    win = np.hamming(FRAME).astype(np.float32)
    spec = np.abs(np.fft.rfft(fr * win, axis=1)).astype(np.float32)
    return np.log1p(spec @ _MEL_FB.T)


def _grid(mel: "np.ndarray", sec: float = GRID_SEC) -> "np.ndarray":
    """把逐帧梅尔谱按 sec 秒聚合，得到时间轴均匀的矩阵"""
    per = max(1, int(round(sec * SR / HOP)))
    if mel.shape[0] < per * 2:
        return np.zeros((0, mel.shape[1]), dtype=np.float32)
    n = mel.shape[0] // per
    return mel[:n * per].reshape(n, per, mel.shape[1]).mean(axis=1)


def _row_norm(a: "np.ndarray") -> "np.ndarray":
    a = a - a.mean(axis=1, keepdims=True)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.maximum(n, 1e-8)


def align_corr(a: "np.ndarray", b: "np.ndarray", max_lag: int,
               tie: float = 0.02) -> Tuple[float, int]:
    """在 ±max_lag 内滑动，返回（最大逐帧平均相关, 最优 lag）。

    倾向小偏移：两侧已经按「时长比例」粗对齐，真正的 lag 应该接近 0。
    频谱长时间恒定的段落（循环伴奏、纯音）会出现多个 lag 相关都饱和的情况，
    这时取 |lag| 最小的那个，避免选到远处的伪峰导致后面 MFCC 比对错位。
    """
    if a.shape[0] < 3 or b.shape[0] < 3:
        return 0.0, 0
    an, bn = _row_norm(a), _row_norm(b)
    scored: List[Tuple[float, int]] = []
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = an[lag:], bn[:an.shape[0] - lag]
        else:
            x, y = an[:an.shape[0] + lag], bn[-lag:]
        n = min(x.shape[0], y.shape[0])
        if n < 3:
            continue
        scored.append((float(np.mean(np.sum(x[:n] * y[:n], axis=1))), lag))
    if not scored:
        return 0.0, 0
    top = max(c for c, _ in scored)
    near = [lag for c, lag in scored if c >= top - tie]
    return max(0.0, top), min(near, key=abs)


def mfcc_frames(x: "np.ndarray") -> "np.ndarray":
    """13 维 MFCC（对对数梅尔谱做 DCT-II）"""
    mel = mel_spec(x)
    if mel.shape[0] == 0:
        return np.zeros((0, 13), dtype=np.float32)
    m = mel - mel.mean(axis=0, keepdims=True)
    n_in, n_out = m.shape[1], 13
    k = np.arange(n_out)[:, None]
    i = np.arange(n_in)[None, :]
    dct = np.cos(np.pi * k * (2 * i + 1) / (2 * n_in)) * math.sqrt(2.0 / n_in)
    return (m @ dct.T).astype(np.float32)


def f0_track(x: "np.ndarray", sr: int = SR) -> "np.ndarray":
    """基频轨迹（自相关法，70~400Hz）；无声/清音帧记 0"""
    fr = _frames(x)
    if fr.shape[0] == 0:
        return np.zeros(0, dtype=np.float32)
    fr = fr - fr.mean(axis=1, keepdims=True)
    spec = np.fft.rfft(fr, n=FRAME * 2, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :FRAME]
    energy = np.maximum(ac[:, :1], 1e-9)
    acn = ac / energy
    lo, hi = int(sr / 400), int(sr / 70)
    seg = acn[:, lo:hi]
    peak = seg.argmax(axis=1) + lo
    val = seg.max(axis=1)
    f0 = sr / np.maximum(peak.astype(np.float32), 1.0)
    return np.where(val > 0.30, f0, 0.0).astype(np.float32)


def _median(arr: "np.ndarray") -> float:
    return float(np.median(arr)) if arr.size else 0.0


# ---------------------------------------------------------------- 基准音频
async def fetch_reference_audio(ncm: Any, sid: int) -> Dict[str, Any]:
    """取「网易云实际播放版本」的直链，作为音频比对的基准。

    **只拿 URL，不下载整首**：交给 ffmpeg 用 input seek（`-ss`/`-t`）按需拉片段，
    实测 40 秒音频约 4~5 秒、几百 KB。

    为什么不做「Range 截一段字节自己解码」：这个账号拿到的 standard 档其实也是 FLAC
    （26MB、br≈950k），从中间截断的 FLAC 片段 ffmpeg 只能解出 7 秒就报
    `invalid residual`（帧边界丢了），而 URL seek 能正常解出完整 40 秒。
    """
    if ncm is None:
        return {"error": "没有可用的 ncm 客户端"}
    url, level, size = "", "", 0
    for lvl in ("standard", "higher", "exhigh"):
        try:
            e = await ncm.song_url(int(sid), lvl)
        except Exception:   # noqa: BLE001
            continue
        if e.get("url") and not e.get("freeTrialInfo"):
            url, level, size = str(e["url"]), lvl, int(e.get("size") or 0)
            break
    if not url:
        return {"error": "网易云没有可完整播放的版本（已下架/无版权/仅试听）"}
    return {"url": url, "url_level": level, "size": size}


# ---------------------------------------------------------------- 主流程
async def verify_file(path: Path, ref: Dict[str, Any], ncm: Any = None,
                      cfg: Optional[Dict[str, Any]] = None,
                      source_is_official: bool = False) -> Report:
    """校验单个文件。

    入参：
      path                 下载完成、尚未打标签的音频文件
      ref                  基准元数据：{"sid", "title", "duration"(秒), "artists":[..],
                           "album", "album_id"}
      ncm                  Ncm 客户端（音频比对要用；传 None 则音频项 skip）
      cfg                  整份配置或 verify 子配置
      source_is_official   本次下载是否来自网易云官方直链（配合 only_third_party）

    出参：Report（见类定义）
    """
    t0 = time.monotonic()
    vcfg = verify_cfg(cfg)
    rep = Report(state="passed", reference={
        "sid": ref.get("sid"), "title": ref.get("title"),
        "audio_ref": False, "audio_ref_level": "", "audio_ref_error": "",
    })

    if not vcfg.get("enabled", True):
        rep.state, rep.error = "skipped", "校验开关已关闭"
        return rep
    if vcfg.get("only_third_party") and source_is_official:
        rep.state, rep.error = "skipped", "仅校验第三方音源（本次为官方直链）"
        return rep
    if not Path(path).exists():
        rep.state, rep.error = "skipped", "文件不存在"
        return rep

    items = [str(x) for x in (vcfg.get("items") or [])]
    local_tags = read_raw_tags(Path(path))
    local_dur = audio_duration(Path(path))
    rep.reference["local_tags"] = dict(local_tags)
    rep.reference["local_duration"] = round(local_dur, 2)

    # ---- 1) 时长
    if "duration" in items:
        ref_dur = float(ref.get("duration") or 0)
        tol = float(vcfg["duration_tolerance"])
        if not ref_dur:
            rep.items.append(Item("duration", "skip", "网易云没有时长信息"))
        elif not local_dur:
            rep.items.append(Item("duration", "skip", "读不出本地时长"))
        else:
            delta = abs(local_dur - ref_dur)
            ok = delta <= tol
            rep.items.append(Item(
                "duration", "pass" if ok else "fail",
                "" if ok else f"本地 {local_dur:.1f}s，网易云 {ref_dur:.1f}s（差 {delta:.1f}s > {tol:.1f}s）",
                local=round(local_dur, 2), ref=round(ref_dur, 2), score=round(1 - min(1.0, delta / max(ref_dur, 1)), 4)))

    # ---- 2) 演唱者
    if "artist" in items:
        artists = ref.get("artists") or []
        if not artists:
            rep.items.append(Item("artist", "skip", "网易云没有歌手信息"))
        elif not local_tags.get("artist"):
            rep.items.append(Item("artist", "skip", "文件没有内嵌歌手标签，无法比对",
                                  local="", ref="/".join(artists)))
        else:
            ok, why = artist_match(local_tags["artist"], artists)
            rep.items.append(Item("artist", "pass" if ok else "fail", "" if ok else why,
                                  local=local_tags["artist"], ref="/".join(artists)))

    # ---- 3) 专辑
    if "album" in items:
        ref_album = str(ref.get("album") or "")
        if not ref_album:
            rep.items.append(Item("album", "skip", "网易云没有专辑信息"))
        elif not local_tags.get("album"):
            rep.items.append(Item("album", "skip", "文件没有内嵌专辑标签，无法比对",
                                  local="", ref=ref_album))
        else:
            ok, score = album_match(local_tags["album"], ref_album,
                                    float(vcfg["album_threshold"]))
            rep.items.append(Item("album", "pass" if ok else "fail",
                                  "" if ok else f"本地「{local_tags['album']}」≠ 网易云「{ref_album}」（相似 {score:.2f}）",
                                  local=local_tags["album"], ref=ref_album, score=score))

    # ---- 4/5) 音频比对（bgm + 声纹）
    want_audio = [k for k in ("bgm", "voiceprint") if k in items]
    if want_audio:
        ref_audio = await fetch_reference_audio(ncm, int(ref.get("sid") or 0)) if ref.get("sid") else {"error": "缺少曲目 id"}
        rep.reference["audio_ref_error"] = str(ref_audio.get("error") or "")
        rep.reference["audio_ref_level"] = str(ref_audio.get("url_level") or "")
        policy = str(vcfg.get("on_missing_reference") or "skip").lower()
        miss_state = "fail" if policy == "fail" else "skip"

        if ref_audio.get("error") or not ref_audio.get("url"):
            why = str(ref_audio.get("error") or "取不到参考音频")
            for k in want_audio:
                rep.items.append(Item(k, miss_state, f"无法比对：{why}"))
        elif not ffmpeg_available():
            for k in want_audio:
                rep.items.append(Item(k, "skip", "环境缺少 ffmpeg/numpy，无法做音频比对"))
        else:
            rep.reference["audio_ref"] = True
            try:
                cmp_ = await asyncio.to_thread(
                    _compare_audio, Path(path), str(ref_audio["url"]), local_dur, vcfg)
            except Exception as e:  # noqa: BLE001  解码/计算异常不该把整首歌判失败
                cmp_ = {k: Item(k, "skip", f"音频比对异常：{type(e).__name__}") for k in want_audio}
            for k in want_audio:
                rep.items.append(cmp_.get(k) or Item(k, "skip", "音频比对没有结果"))

    if not rep.items:
        rep.state, rep.error = "skipped", "没有可比对的项"
    elif any(i.state == "fail" for i in rep.items):
        rep.state = "failed"
    else:
        rep.state = "passed"

    rep.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return rep


def _compare_audio(local_path: Path, ref_src: str, local_dur: float,
                   vcfg: Dict[str, Any]) -> Dict[str, Item]:
    """本地文件 vs 官方播放流：算 bgm 与声纹两项。

    两侧取**同一时间点**（按本地时长的 REF_POSITION 处、往前留对齐窗口），
    再由谱图互相关在 ±MAX_LAG_SEC 内微调 —— 同一首歌两侧天然对齐，lag 通常接近 0；
    换版本（Live/翻唱/伴奏）则会整体错位或明显掉相关。
    """
    out: Dict[str, Item] = {}
    skip = max(0.0, local_dur * REF_POSITION - MAX_LAG_SEC) if local_dur else 0.0
    span = REF_PROBE_SEC + MAX_LAG_SEC * 2
    loc = decode_pcm(local_path, skip=skip, limit=span)
    ref_pcm = decode_pcm(ref_src, skip=skip, limit=span)
    if loc is None or loc.size < SR * 4:
        msg = "本地音频解码失败或过短"
        return {k: Item(k, "skip", msg) for k in ("bgm", "voiceprint")}
    if ref_pcm is None or ref_pcm.size < SR * 4:
        msg = "官方播放流解码失败或过短（网络/防盗链）"
        return {k: Item(k, "skip", msg) for k in ("bgm", "voiceprint")}

    loc_mel, ref_mel = _grid(mel_spec(loc)), _grid(mel_spec(ref_pcm))
    max_lag = int(round(MAX_LAG_SEC / GRID_SEC))
    corr, lag = align_corr(loc_mel, ref_mel, max_lag)
    bgm_th = float(vcfg["bgm_threshold"])
    ok_bgm = corr >= bgm_th
    out["bgm"] = Item(
        "bgm", "pass" if ok_bgm else "fail",
        "" if ok_bgm else f"音频内容与官方版本不符（谱图相关 {corr:.2f} < {bgm_th:.2f}）",
        local=f"本地片段 {len(loc) / SR:.0f}s", ref=f"官方片段 {len(ref_pcm) / SR:.0f}s",
        score=corr)

    # ---- 声纹：对齐后 MFCC 帧级余弦相似度 + 基频中位数
    loc_mf, ref_mf = mfcc_frames(loc), mfcc_frames(ref_pcm)
    if loc_mf.shape[0] < 10 or ref_mf.shape[0] < 10:
        out["voiceprint"] = Item("voiceprint", "skip", "音频过短，无法提取声纹特征")
        return out
    shift = lag * max(1, int(round(GRID_SEC * SR / HOP)))
    if shift >= 0:
        a, b = loc_mf[shift:], ref_mf[:max(0, loc_mf.shape[0] - shift)]
    else:
        a, b = loc_mf[:max(0, loc_mf.shape[0] + shift)], ref_mf[-shift:]
    n = min(a.shape[0], b.shape[0])
    if n < 10:
        out["voiceprint"] = Item("voiceprint", "skip", "对齐后重叠太少")
        return out
    an = _row_norm(a[:n])
    bn = _row_norm(b[:n])
    frame_cos = np.sum(an * bn, axis=1)
    cos_med = float(np.median(frame_cos))

    f0l, f0r = f0_track(loc), f0_track(ref_pcm)
    lv = f0l[f0l > 0]
    rv = f0r[f0r > 0]
    voiced = (lv.size / max(1, f0l.size)) if f0l.size else 0.0
    if voiced < 0.15 or rv.size < 20 or lv.size < 20:
        out["voiceprint"] = Item("voiceprint", "skip",
                                 "有效人声帧太少（可能是纯音乐），声纹无法比对",
                                 local=round(voiced, 2))
        return out
    ml, mr = _median(lv), _median(rv)
    pitch_rel = abs(ml - mr) / max(mr, 1e-6)
    vth = float(vcfg["voiceprint_threshold"])
    pth = float(vcfg["pitch_tolerance"])
    ok_v = cos_med >= vth and pitch_rel <= pth
    why = []
    if cos_med < vth:
        why.append(f"音色相似度 {cos_med:.2f} < {vth:.2f}")
    if pitch_rel > pth:
        why.append(f"音高差 {pitch_rel * 100:.1f}% > {pth * 100:.0f}%（本地 {ml:.0f}Hz / 官方 {mr:.0f}Hz）")
    out["voiceprint"] = Item(
        "voiceprint", "pass" if ok_v else "fail",
        "" if ok_v else "歌手声音与官方版本不符：" + "，".join(why),
        local=f"MFCC {cos_med:.3f} / F0 {ml:.0f}Hz",
        ref=f"MFCC 基准 / F0 {mr:.0f}Hz",
        score=cos_med)
    return out


# ---------------------------------------------------------------- 结果落盘
def log_report(rep: Report, extra: Optional[Dict[str, Any]] = None,
               path: str = "/data/verify.jsonl") -> None:
    """把完整报告追加到 JSONL（便于事后统计误报 / 回看）"""
    try:
        import json
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **(extra or {}), **rep.to_dict()}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:   # noqa: BLE001  日志写不进去不能影响主流程
        pass
