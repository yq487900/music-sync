"""音质协商：配置链 × 歌曲最高档 × 账号可用档。"""
from __future__ import annotations

from typing import List, Optional

# 保真度从低到高
LEVEL_ORDER = ["standard", "higher", "exhigh", "lossless", "hires",
               "jyeffect", "sky", "dolby", "vivid", "jymaster"]


def rank(level: Optional[str]) -> int:
    """未知档位返回 -1（无约束），便于兼容网易云以后新增的档位"""
    if not level:
        return -1
    try:
        return LEVEL_ORDER.index(str(level))
    except ValueError:
        return -1


def pick_level(privilege: Optional[dict], chain: List[str]) -> str:
    """chain 按高→低排列，返回第一个同时不超过歌曲上限与账号上限的档位"""
    priv = privilege or {}
    song_max = rank(priv.get("maxBrLevel"))
    acc_max = rank(priv.get("dlLevel"))
    for lvl in chain:
        r = rank(lvl)
        if r < 0:
            continue
        if song_max >= 0 and r > song_max:
            continue
        if acc_max >= 0 and r > acc_max:
            continue
        return lvl
    return chain[-1] if chain else "standard"


def chain_from(level: str, chain: List[str]) -> List[str]:
    """返回 level 及其以下的降档序列，用于取流失败时逐档重试"""
    r = rank(level)
    out = [x for x in chain if rank(x) >= 0 and rank(x) <= (r if r >= 0 else 10 ** 9)]
    return out or [level]
