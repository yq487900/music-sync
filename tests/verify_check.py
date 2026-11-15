"""下载一致性校验的独立测试工具（不依赖 Web 界面、不改数据库、不下载歌）。

用法（容器内）：
    python /tmp/verify_check.py --file /data/tmp/x.flac --sid 186016   # 单文件
    python /tmp/verify_check.py --library --limit 20                   # 批量复核已下载曲目
    python /tmp/verify_check.py --library --only-failed --limit 50     # 只跑上次没过的
    python /tmp/verify_check.py --self-test                            # 离线自测（不联网）

批量复核的用途：拿曲库里**已经正常播放的**歌跑一遍，统计通过率 = 误报率。
注意：这些文件已经被写过我们的标签，所以「演唱者/专辑」两项必然通过、没有区分力；
真正有参考价值的是「时长 / 音乐 bgm / 歌手声纹」三项。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, "/app")     # 从 /tmp 拷进去跑时（tests/run.sh）也能 import app 包

from app.ncm import Ncm                                    # noqa: E402
from app.verify import (Item, Report, album_match, align_corr, artist_match,  # noqa: E402
                        decode_pcm, mel_spec, verify_cfg, verify_file, _grid)


# ---------------------------------------------------------------- 输出
ICON = {"pass": "✅", "fail": "❌", "skip": "➖"}


def show(rep: Report, title: str, path: str, sid: int = 0) -> None:
    ref = rep.reference or {}
    print("─" * 74)
    print(f" 曲目：{title}  (sid={sid})")
    print(f" 文件：{path}")
    if ref.get("audio_ref"):
        print(f" 基准：网易云官方 {ref.get('audio_ref_level') or '?'} 档片段（已取到）")
    elif ref.get("audio_ref_error"):
        print(f" 基准：取不到官方播放流 —— {ref['audio_ref_error']}")
    print("─" * 74)
    print(f" {'比对项':<10}{'结论':<6}{'本地':<26}{'网易云':<24}{'相似度'}")
    for it in rep.items:
        loc = str(it.local if it.local is not None else "—")[:24]
        r = str(it.ref if it.ref is not None else "—")[:22]
        sc = "—" if it.score is None else f"{it.score:.3f}"
        print(f" {it.label:<10}{ICON.get(it.state, '?')}     {loc:<26}{r:<24}{sc}")
        if it.detail:
            print(f"   └ {it.detail}")
    print("─" * 74)
    print(f" 结论：{rep.summary()}   （耗时 {rep.elapsed_ms} ms）")
    print()


# ---------------------------------------------------------------- 数据来源
def _load_cookie(cfg_path: str = "/data/config.json") -> str:
    try:
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        return str(((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or "")
    except Exception:   # noqa: BLE001
        return ""


async def ref_from_api(ncm: Ncm, sid: int) -> dict:
    """按 sid 现取网易云元数据（不依赖数据库）"""
    d = await ncm.get("/song/detail", ids=str(sid))
    songs = d.get("songs") or []
    if not songs:
        raise SystemExit(f"网易云查不到曲目 {sid}")
    s = songs[0]
    return {
        "sid": sid,
        "title": str(s.get("name") or sid),
        "duration": float(s.get("dt") or 0) / 1000.0,
        "artists": [str(a.get("name")) for a in (s.get("ar") or []) if a.get("name")],
        "album": str((s.get("al") or {}).get("name") or ""),
    }


def library_rows(limit: int, offset: int, only_failed: bool) -> list:
    """从数据库取已下载曲目：（sid, 标题, 歌手, 时长, 文件路径, 上次结论）"""
    from app.db.models import SessionLocal, Track
    db = SessionLocal()
    try:
        q = db.query(Track).filter(Track.platform == "netease")
        if only_failed:
            q = q.filter(Track.verify_state == "failed")
        rows = q.order_by(Track.id.asc()).offset(offset).all()
        out = []
        for tr in rows:
            fp = str(tr.file_path or "")
            if not fp or not Path(fp).exists():
                continue
            detail = tr.track_metadata if isinstance(tr.track_metadata, dict) else {}
            out.append({
                "sid": int(tr.platform_track_id or 0), "title": tr.title or "",
                "artist": tr.artist or "", "duration": float(tr.duration or 0),
                "album": tr.album or "", "file": fp, "prev": tr.verify_state or "",
                "detail": detail,
            })
            if len(out) >= limit:
                break
        return out
    finally:
        db.close()


def ref_from_row(row: dict) -> dict:
    detail = row.get("detail") or {}
    artists = [str(a.get("name")) for a in (detail.get("ar") or []) if isinstance(a, dict) and a.get("name")]
    return {
        "sid": row["sid"], "title": row["title"], "duration": row["duration"],
        "artists": artists or [x.strip() for x in str(row.get("artist") or "").split(" / ") if x.strip()],
        "album": row["album"],
    }


# ---------------------------------------------------------------- 离线自测
def self_test() -> int:
    """合成音频 + 文本规则的自测：不联网、不依赖曲库"""
    print("=" * 74)
    print(" 离线自测（合成音频 + 文本规则）")
    print("=" * 74)
    fails = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal fails
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  {extra}" if extra else ""))
        fails += 0 if ok else 1

    # ---- 1) 文本规则
    check("演唱者：完全一致", artist_match("周杰伦", ["周杰伦"])[0])
    check("演唱者：多歌手写主唱", artist_match("周杰伦", ["周杰伦", "费玉清"])[0])
    check("演唱者：包含关系", artist_match("周杰伦 Jay Chou", ["周杰伦"])[0])
    check("演唱者：斜杠拆分", artist_match("周杰伦/费玉清", ["费玉清"])[0])
    check("演唱者：完全不同应拒绝", not artist_match("林俊杰", ["周杰伦"])[0])
    check("专辑：完全一致", album_match("范特西", "范特西", 0.82)[0])
    check("专辑：(Deluxe) 后缀", album_match("范特西 (Deluxe Edition)", "范特西", 0.82)[0])
    check("专辑：完全不同应拒绝", not album_match("七里香", "范特西", 0.82)[0])

    # ---- 2) 音频特征
    import shutil
    if not shutil.which("ffmpeg"):
        print("  ➖ 本机没有 ffmpeg，跳过音频部分")
        return 1 if fails else 0
    try:
        import numpy as np
    except Exception:   # noqa: BLE001
        print("  ➖ 没有 numpy，跳过音频部分")
        return 1 if fails else 0

    with tempfile.TemporaryDirectory() as td:
        def synth_seq(name: str, freqs: list, seg: float = 1.5, delay: float = 0.0) -> Path:
            """拼一段「音调序列」：每段换频率，使各时刻频谱不同（纯正弦频谱恒定，对齐会退化）"""
            dst = Path(td) / name
            cmd = ["ffmpeg", "-v", "quiet", "-y"]
            inputs: list = []
            if delay > 0:
                inputs += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={delay}"]
            for f in freqs:
                inputs += ["-f", "lavfi", "-i", f"sine=frequency={f}:duration={seg}:sample_rate=44100"]
            labels = "".join(f"[{i}:a]" for i in range(len(freqs) + (1 if delay > 0 else 0)))
            n = len(freqs) + (1 if delay > 0 else 0)
            cmd += inputs + ["-filter_complex", f"{labels}concat=n={n}:v=0:a=1",
                             "-ac", "1", str(dst)]
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
            return dst

        freqs = [220.0, 330.0, 262.0, 392.0, 294.0, 349.0, 233.0, 311.0]
        a = synth_seq("a.wav", freqs)
        a_delay = synth_seq("a_delay.wav", freqs, delay=1.0)
        b = synth_seq("b.wav", [247.0, 370.0, 208.0, 415.0, 277.0, 330.0, 196.0, 440.0])

        pa = decode_pcm(a)
        check("ffmpeg 解码：得到约 12 秒音频",
              pa is not None and abs(pa.size / 8000 - 12.0) < 0.8,
              f"{0 if pa is None else round(pa.size / 8000, 2)}s")

        if pa is not None:
            ga = _grid(mel_spec(pa))
            check("梅尔谱：帧数与时长为正比", ga.shape[0] >= 20, f"{ga.shape}")

            self_corr, self_lag = align_corr(ga, ga, 4)
            check("谱图对齐：自己与自己相关 ≈ 1", self_corr > 0.98, f"{self_corr:.3f} lag={self_lag}")

            pb = decode_pcm(b)
            if pb is not None:
                gb = _grid(mel_spec(pb))
                other, _ = align_corr(ga, gb, 4)
                check("谱图对齐：不同音频相关明显更低", other < 0.8,
                      f"同={self_corr:.3f} / 异={other:.3f}")

            pdelay = decode_pcm(a_delay)
            if pdelay is not None:
                gd = _grid(mel_spec(pdelay))
                d_corr, d_lag = align_corr(gd, ga, 4)
                # 前面多了 1 秒静音 → 本地要比参考晚 1 秒 = 2 格（0.5s/格）
                check("谱图对齐：能纠正 1 秒偏移", d_corr > 0.9 and d_lag == 2,
                      f"相关={d_corr:.3f} lag={d_lag}（期望 2）")

    print()
    print(f" 结果：{'全部通过' if not fails else f'{fails} 项失败'}")
    return 1 if fails else 0


# ---------------------------------------------------------------- 主流程
async def main_async(args) -> int:
    cfg = {"verify": {}}          # 用默认阈值；需要改就传 --bgm-threshold 之类
    if args.bgm_threshold is not None:
        cfg["verify"]["bgm_threshold"] = args.bgm_threshold
    if args.voice_threshold is not None:
        cfg["verify"]["voiceprint_threshold"] = args.voice_threshold
    if args.tolerance is not None:
        cfg["verify"]["duration_tolerance"] = args.tolerance
    if args.on_missing:
        cfg["verify"]["on_missing_reference"] = args.on_missing

    cookie = args.cookie or _load_cookie(args.config)
    ncm = Ncm(cookie=cookie, base=args.ncm_base) if cookie or args.ncm_base else Ncm(base=args.ncm_base)

    # ---- 单文件
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"文件不存在：{path}")
            return 2
        if args.sid:
            ref = await ref_from_api(ncm, int(args.sid))
        elif args.track_id:
            from app.db.models import SessionLocal, Track
            db = SessionLocal()
            try:
                tr = db.query(Track).filter_by(id=int(args.track_id)).first()
                if tr is None:
                    print(f"曲库里没有 id={args.track_id}")
                    return 2
                ref = ref_from_row({
                    "sid": int(tr.platform_track_id or 0), "title": tr.title or "",
                    "duration": float(tr.duration or 0), "album": tr.album or "",
                    "artist": tr.artist or "", "detail": tr.track_metadata or {},
                })
            finally:
                db.close()
        else:
            print("需要 --sid（网易云曲目 id）")
            return 2
        rep = await verify_file(path, ref, ncm, cfg)
        await ncm.close()
        if args.json:
            print(json.dumps({"track": ref, "report": rep.to_dict()}, ensure_ascii=False, indent=2))
        else:
            show(rep, f"{ref['title']} — {'/'.join(ref['artists'])}", str(path), ref["sid"])
        return 0 if rep.passed else 1

    # ---- 批量复核
    if args.library:
        rows = library_rows(args.limit, args.offset, args.only_failed)
        if not rows:
            print("曲库里没有可复核的文件（file_path 为空或文件不存在）")
            return 2
        print(f"共 {len(rows)} 首待复核，阈值：bgm={verify_cfg(cfg)['bgm_threshold']} "
              f"声纹={verify_cfg(cfg)['voiceprint_threshold']} 时长容差={verify_cfg(cfg)['duration_tolerance']}s")
        print()
        stats = {"passed": 0, "failed": 0, "skipped": 0}
        bad: list = []
        t0 = time.monotonic()
        for i, row in enumerate(rows, 1):
            try:
                rep = await verify_file(Path(row["file"]), ref_from_row(row), ncm, cfg)
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(rows)}] {row['title']} 异常：{type(e).__name__}: {e}")
                stats["skipped"] += 1
                continue
            stats[rep.state] = stats.get(rep.state, 0) + 1
            mark = {"passed": "✅", "failed": "❌", "skipped": "➖"}[rep.state]
            items = " ".join(f"{it.label}{ICON.get(it.state, '?')}" for it in rep.items)
            print(f"[{i}/{len(rows)}] {mark} {row['title'][:28]:<30} {items}")
            if rep.state == "failed":
                bad.append((row, rep))
        await ncm.close()
        dt = time.monotonic() - t0
        total = sum(stats.values())
        print()
        print("=" * 74)
        print(f" 通过 {stats.get('passed', 0)} / 总计 {total}"
              f"（失败 {stats.get('failed', 0)}，跳过 {stats.get('skipped', 0)}）"
              f"  耗时 {dt:.1f}s（{dt / max(1, total):.2f}s/首）")
        if total:
            print(f" 误报率（把正常歌判失败的比例）：{stats.get('failed', 0) / total * 100:.1f}%")
        if bad and args.verbose:
            print()
            for row, rep in bad:
                show(rep, row["title"], row["file"], row["sid"])
        elif bad:
            print(" 判失败的曲目（加 --verbose 看明细）：")
            for row, rep in bad:
                print(f"   · {row['title'][:30]:<32} {rep.summary()}")
        print("=" * 74)
        return 0

    print(__doc__)
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description="下载一致性校验独立测试工具")
    ap.add_argument("--file", help="要校验的音频文件")
    ap.add_argument("--sid", type=int, help="网易云曲目 id（配合 --file）")
    ap.add_argument("--track-id", type=int, help="曲库里的曲目 id（配合 --file，从库取基准）")
    ap.add_argument("--library", action="store_true", help="批量复核曲库里已下载的曲目")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--only-failed", action="store_true", help="只复核上次校验失败的")
    ap.add_argument("--self-test", action="store_true", help="离线自测（不联网）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--verbose", action="store_true", help="打印判失败曲目的完整明细")
    ap.add_argument("--ncm-base", default=os.environ.get("NCM_API", "http://127.0.0.1:3000"))
    ap.add_argument("--cookie", default="", help="网易云 cookie（默认从 /data/config.json 读）")
    ap.add_argument("--config", default="/data/config.json")
    ap.add_argument("--bgm-threshold", type=float, help="覆盖 bgm 阈值（标定用）")
    ap.add_argument("--voice-threshold", type=float, help="覆盖声纹阈值（标定用）")
    ap.add_argument("--tolerance", type=float, help="覆盖时长容差（秒）")
    ap.add_argument("--on-missing", choices=("skip", "fail"), help="拿不到参考音频时的处理")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
