# -*- coding: utf-8 -*-
"""模型识别与匹配。

第三方中转经常给模型改名（加前缀、加日期、加 :free 之类），
这里按精确、归一化、模糊三层策略去官方参数库里找对应型号。
"""
import re
import difflib

# -cc 是中转常用的后缀，表示 Claude Code 式转发（如 minimax-m3-cc 对应 MiniMax-M3）
_SUFFIXES = ("-preview", "-latest", "-stable", "-free", "-it", "-instruct",
             "-thinking", "-cc")
_DATE8 = re.compile(r"-\d{8}$")
_DATE4 = re.compile(r"-(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")

# 同一个型号常同时出现在厂商官方文件和各网关文件里，
# 分数相同时优先采信厂商官方数据。
_FIRST_PARTY = {
    "openai", "anthropic", "google", "xai", "deepseek",
    "moonshotai", "minimax", "zai", "mistral", "ant-ling",
    "moonshotai-cn", "minimax-cn", "zai-coding-cn",
}


def normalize(s):
    s = (s or "").strip().lower()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if ":" in s:
        s = s.split(":", 1)[0]
    s = s.replace("_", "-")
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
    s = _DATE8.sub("", s)
    s = _DATE4.sub("", s)
    return s


class MatchResult:
    __slots__ = ("entry", "score")

    def __init__(self, entry, score):
        self.entry = entry
        self.score = score


def _norm_index(catalog):
    """取归一化索引。Catalog 会缓存，传进来的替身对象则临时建一份。"""
    getter = getattr(catalog, "norm_index", None)
    if callable(getter):
        return getter()
    idx = {}
    for e in catalog.entries:
        idx.setdefault(normalize(e.mid), []).append(e)
    return idx


def match(catalog, model_id, prefer_api=None, top=5, fuzzy_threshold=0.70):
    """返回候选列表，第一项是最优匹配。找不到时列表为空。"""
    raw = (model_id or "").strip()
    if not raw:
        return []
    norm = normalize(raw)
    by_norm = _norm_index(catalog)

    results = []
    seen = set()

    def add(entry, score):
        key = id(entry)
        if key in seen:
            return
        seen.add(key)
        results.append((entry, score))

    # 1. 精确 id
    exact = [e for e in catalog.entries if e.mid == raw]
    if not exact and "/" in raw:
        tail = raw.rsplit("/", 1)[-1]
        exact = [e for e in catalog.entries if e.mid == tail]
    for e in exact:
        bonus = 0.02 if (prefer_api and e.api == prefer_api) else 0.0
        add(e, 1.0 + bonus)

    # 2. 归一化相等
    for e in by_norm.get(norm, []):
        bonus = 0.01 if (prefer_api and e.api == prefer_api) else 0.0
        add(e, 0.97 + bonus)

    # 3. 模糊
    best_exact = max((s for _e, s in results), default=0.0)
    if not results or best_exact < 1.0:
        scored = []
        for cn in by_norm.keys():
            best = difflib.SequenceMatcher(None, norm, cn).ratio()
            # 一方是另一方的前缀也算高分（比如 gpt-5.6 vs gpt-5.6-mini）
            if cn.startswith(norm) or norm.startswith(cn):
                l = min(len(norm), len(cn))
                denom = max(len(norm), len(cn)) or 1
                p = 0.55 + 0.4 * (l / denom)
                if p > best:
                    best = p
            if best >= fuzzy_threshold:
                scored.append((cn, best))
        scored.sort(key=lambda x: -x[1])
        for cn, sc in scored[:top * 3]:
            bonus = 0.0
            for e in by_norm[cn]:
                if prefer_api and e.api == prefer_api:
                    bonus = 0.02
                    break
            add(e, round(min(sc + bonus, 0.96), 3))
            if len(results) >= top:
                break

    # 排序：分数优先，同分时厂商官方文件优先，最后保持目录顺序
    ranked = sorted(
        enumerate(results),
        key=lambda t: (-t[1][1],
                       0 if t[1][0].vendor in _FIRST_PARTY else 1,
                       t[0]),
    )
    return [MatchResult(e, s) for _, (e, s) in ranked[:top]]


def score_label(score):
    if score >= 1.0:
        return "精确"
    if score >= 0.9:
        return "很可能"
    if score >= 0.75:
        return "疑似"
    return "低"
