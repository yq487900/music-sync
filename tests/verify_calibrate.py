"""一致性校验阈值标定工具：拿曲库里**真实能正常播**的歌跑一遍，看分数分布。

正样本：文件用自己的 netease_id 做基准 → 都应该通过（用来定「误杀」上限）
负样本：文件配**别人**的 sid 做基准 → 都应该判失败（用来定「漏放」下限）

用法（容器内）：
    python /app/tests/verify_calibrate.py --sample 12
    python /app/tests/verify_calibrate.py --sample 20 --root /music/musics --json

注意：只读文件、只调网易云接口，不写库、不改文件。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, "/app")

from app.ncm import Ncm                                  # noqa: E402
from app.verify import verify_file      # noqa: E402

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape", ".wv"}


def read_sid(path: Path) -> int:
    """读文件标签里的网易云曲目 id（我们打标签时写入的 netease_id / NETEASE_ID）"""
    try:
        from mutagen import File as MutagenFile
        f = MutagenFile(str(path))
        if f is None:
            return 0
        tags = getattr(f, "tags", None)
        if not tags:
            return 0
        for key in list(tags.keys()):
            if "NETEASE_ID" not in str(key).upper():
                continue
            val = tags[key]
            txt = ""
            if hasattr(val, "text"):
                txt = str(val.text[0]) if val.text else ""
            elif isinstance(val, (list, tuple)) and val:
                first = val[0]
                txt = first.decode("utf-8", "ignore") if isinstance(first, bytes) else str(first)
            else:
                txt = str(val)
            digits = "".join(ch for ch in txt if ch.isdigit())
            return int(digits) if digits else 0
    except Exception:   # noqa: BLE001
        return 0
    return 0


def scan(root: Path, limit: int, seed: int = 0) -> list:
    files = [p for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXT and p.is_file()]
    random.Random(seed).shuffle(files)
    out: list = []
    for p in files:
        sid = read_sid(p)
        if sid:
            out.append({"path": str(p), "sid": sid, "title": p.stem})
        if len(out) >= limit:
            break
    return out


async def ref_of(ncm: Ncm, sid: int) -> dict:
    d = await ncm.get("/song/detail", ids=str(sid))
    songs = d.get("songs") or []
    if not songs:
        return {}
    s = songs[0]
    return {
        "sid": sid, "title": str(s.get("name") or sid),
        "duration": float(s.get("dt") or 0) / 1000.0,
        "artists": [str(a.get("name")) for a in (s.get("ar") or []) if a.get("name")],
        "album": str((s.get("al") or {}).get("name") or ""),
    }


def scores(rep) -> dict:
    out = {}
    for it in rep.items:
        out[it.key] = {"state": it.state, "score": it.score, "detail": it.detail}
    return out


async def run(args) -> int:
    root = Path(args.root)
    if not root.exists():
        print(f"目录不存在：{root}")
        return 2
    rows = scan(root, args.sample, args.seed)
    if not rows:
        print("没找到带 netease_id 标签的音频文件")
        return 2
    print(f"取到 {len(rows)} 首带网易云 id 的曲目（目录 {root}）\n")

    ncm = Ncm(cookie=args.cookie, base=args.ncm_base)
    cfg = {"verify": {}}
    if args.bgm_threshold is not None:
        cfg["verify"]["bgm_threshold"] = args.bgm_threshold
    if args.voice_threshold is not None:
        cfg["verify"]["voiceprint_threshold"] = args.voice_threshold

    pos: list = []
    neg: list = []
    try:
        # ---- 正样本
        print("=" * 78)
        print(" 正样本（文件 vs 自己的 sid）—— 期望全部通过")
        print("=" * 78)
        refs = {}
        for i, row in enumerate(rows, 1):
            ref = await ref_of(ncm, row["sid"])
            if not ref:
                print(f" [{i}] {row['title'][:30]:<32} 跳过（网易云查不到 {row['sid']}）")
                continue
            refs[row["sid"]] = ref
            rep = await verify_file(Path(row["path"]), ref, ncm, cfg)
            s = scores(rep)
            bgm = s.get("bgm", {}).get("score")
            voi = s.get("voiceprint", {}).get("score")
            dur = s.get("duration", {})
            mark = {"passed": "✅", "failed": "❌", "skipped": "➖"}[rep.state]
            print(f" {mark} [{i}] {row['title'][:28]:<30} bgm={bgm if bgm is not None else '—':<6} "
                  f"声纹={voi if voi is not None else '—':<6} 时长={dur.get('state')}")
            if rep.state == "failed":
                print(f"      ↳ {rep.summary()}")
            pos.append({"sid": row["sid"], "title": row["title"], "state": rep.state,
                        "bgm": bgm, "voiceprint": voi, "duration": dur.get("score"),
                        "items": s})

        # ---- 负样本（错位配对：同专辑相邻曲最像，是最难的负样本）
        if args.negatives and len(rows) >= 2:
            print()
            print("=" * 78)
            print(" 负样本（文件 vs 下一个文件的 sid）—— 期望全部判失败")
            print("=" * 78)
            for i in range(min(args.negatives, len(rows) - 1)):
                a, b = rows[i], rows[i + 1]
                ref = refs.get(b["sid"]) or await ref_of(ncm, b["sid"])
                if not ref:
                    continue
                rep = await verify_file(Path(a["path"]), ref, ncm, cfg)
                s = scores(rep)
                bgm = s.get("bgm", {}).get("score")
                voi = s.get("voiceprint", {}).get("score")
                mark = {"passed": "❌漏放", "failed": "✅抓到", "skipped": "➖"}[rep.state]
                print(f" {mark:<7} {a['title'][:24]:<26} vs {ref['title'][:20]:<22} "
                      f"bgm={bgm if bgm is not None else '—':<6} 声纹={voi if voi is not None else '—'}")
                neg.append({"file_sid": a["sid"], "ref_sid": b["sid"], "state": rep.state,
                            "bgm": bgm, "voiceprint": voi})
    finally:
        await ncm.close()

    # ---- 汇总
    def pct(vals, p):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        k = max(0, min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1)))))
        return vals[k]

    print()
    print("=" * 78)
    print(" 分数分布")
    print("=" * 78)
    for key, label in (("bgm", "音乐 bgm"), ("voiceprint", "歌手声纹")):
        pv = [x[key] for x in pos if x.get(key) is not None]
        nv = [x[key] for x in neg if x.get(key) is not None]
        if pv:
            print(f" {label} 正样本: 最小 {min(pv):.3f} / p5 {pct(pv, 5):.3f} / "
                  f"中位 {pct(pv, 50):.3f} / 最大 {max(pv):.3f}  (n={len(pv)})")
        if nv:
            print(f" {label} 负样本: 最小 {min(nv):.3f} / 中位 {pct(nv, 50):.3f} / "
                  f"最大 {max(nv):.3f}  (n={len(nv)})")
        if pv and nv:
            gap_lo, gap_hi = max(nv), min(pv)
            if gap_hi > gap_lo:
                print(f"   → 建议阈值 {gap_lo + (gap_hi - gap_lo) * 0.4:.2f} ~ {gap_lo + (gap_hi - gap_lo) * 0.6:.2f}"
                      f"（正样本最低 {gap_hi:.3f} / 负样本最高 {gap_lo:.3f}，可分）")
            else:
                print(f"   → ⚠ 正负样本分数重叠（正最低 {gap_hi:.3f} < 负最高 {gap_lo:.3f}），需调特征或阈值")

    passed = len([x for x in pos if x["state"] == "passed"])
    failed = len([x for x in pos if x["state"] == "failed"])
    print()
    print(f" 正样本通过 {passed}/{len(pos)}，误杀 {failed} 首"
          + (f"（{failed / len(pos) * 100:.0f}%）" if pos else ""))
    if neg:
        caught = len([x for x in neg if x["state"] == "failed"])
        print(f" 负样本抓到 {caught}/{len(neg)}" + (f"，漏放 {len(neg) - caught} 首" if caught < len(neg) else "，全部抓到"))

    if args.json:
        print()
        print(json.dumps({"positive": pos, "negative": neg}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="一致性校验阈值标定")
    ap.add_argument("--root", default="/music/musics")
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--negatives", type=int, default=12, help="负样本数量（0=不跑）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--ncm-base", default="http://127.0.0.1:3000")
    ap.add_argument("--cookie", default="")
    ap.add_argument("--bgm-threshold", type=float)
    ap.add_argument("--voice-threshold", type=float)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.cookie:
        try:
            cfg = json.loads(Path("/data/config.json").read_text(encoding="utf-8"))
            args.cookie = str(((cfg.get("platforms") or {}).get("netease") or {}).get("cookie") or "")
        except Exception:   # noqa: BLE001
            args.cookie = ""
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
