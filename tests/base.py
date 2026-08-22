# -*- coding: utf-8 -*-
"""测试用的公共夹具。

参数库一律强制用仓库里的 snapshot，避免不同机器上装的 pi 版本不同导致断言飘。
断言也尽量从参数库里现取现用，快照升级后不会无故变红。
"""
import functools

import catalog as catalog_mod


@functools.lru_cache(maxsize=1)
def snapshot_catalog():
    return catalog_mod.Catalog(prefer_snapshot=True)


def pick(pred, limit=1):
    """从参数库里挑符合条件的条目。"""
    out = []
    for e in snapshot_catalog().entries:
        if pred(e):
            out.append(e)
            if len(out) >= limit:
                break
    return out


def pick_one(pred):
    got = pick(pred, 1)
    return got[0] if got else None
