# -*- coding: utf-8 -*-
"""一次性脚本：整理 ~/.pi/agent/models.json。

默认只做演练并打印改动清单，加 --write 才真正落盘（落盘前自动备份并做三道校验）。
整理规则见 plan() 里的注释，全部来自《pi 模型服务商配置指南》里的经验。
"""
import argparse
import copy
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import catalog as catalog_mod            # noqa: E402
import matcher                            # noqa: E402
import store                              # noqa: E402
import validator                          # noqa: E402

CAT = catalog_mod.Catalog()

# 要删掉哪些服务商、要把谁合并进谁，都从命令行传，脚本本身不写死任何人的站点
DROP_PROVIDERS = []
MERGE = []
# OpenAI 格式中转的兼容预设（pi 把未知中转当标准 OpenAI，会发 developer 角色和
# max_completion_tokens，多数中转两样都不认）
OPENAI_PRESET = {"supportsDeveloperRole": False,
                 "maxTokensField": "max_tokens",
                 "supportsFinishReason": False}
ANTHROPIC_PRESET = {"supportsEagerToolInputStreaming": False}
# 只在 Claude 协议下有意义的兼容键，写在 OpenAI 服务商级属于放错地方
ANTHROPIC_ONLY_KEYS = {"supportsEagerToolInputStreaming", "allowEmptySignature",
                       "forceAdaptiveThinking", "supportsTemperature",
                       "supportsCacheControlOnTools", "supportsStrictTools",
                       "supportsToolReferences"}


# pi 给模型的兜底默认值，网关文件里经常直接拿它当占位，不能当成真实上限
PLACEHOLDER_MAX = {16384, 4096, 8192}


def reference(mid):
    """挑一个可信的官方参考条目。

    同一个模型在参数库里有厂商官方文件和一堆网关副本，网关副本的 maxTokens
    常常是 16384 这种占位值（例如 minimaxai/minimax-m3 在 nvidia 文件里写 16384，
    而厂商官方是 128000）。所以优先采信厂商官方文件，其次采信多数一致的写法。
    """
    cands = [r for r in matcher.match(CAT, mid, top=8, fuzzy_threshold=0.9)
             if r.score >= 0.9]
    if not cands:
        return None
    first = [r.entry.model for r in cands
             if r.entry.vendor in matcher._FIRST_PARTY]
    if first:
        return first[0]
    pairs = {}
    for r in cands:
        m = r.entry.model
        key = (m.get("contextWindow"), m.get("maxTokens"))
        pairs.setdefault(key, []).append(m)
    best = max(pairs.values(),
               key=lambda ms: (len(ms), ms[0].get("maxTokens") or 0))
    return best[0]


def align_model(m, changes, where):
    """按官方参数库校准一个模型的数值。

    只往安全方向改：配置值比官方大就压回官方（虚标会让 pi 发超限请求），
    比官方小就保留（中转可能真有更低的暗上限，也可能是免费档位的限制）。
    """
    off = reference(m.get("id", ""))
    if not off:
        changes.append("%s %s：官方参数库里没有，参数按原样保留" % (where, m.get("id")))
        return
    for key, zh in (("contextWindow", "上下文"), ("maxTokens", "最大输出")):
        cur, std = m.get(key), off.get(key)
        if not (isinstance(cur, int) and isinstance(std, int) and cur > std):
            continue
        if key == "maxTokens" and std in PLACEHOLDER_MAX:
            changes.append("%s %s：最大输出 %s 大于参考值 %s，但参考值像占位默认，保留原值"
                           % (where, m.get("id"), format(cur, ","), format(std, ",")))
            continue
        m[key] = std
        changes.append("%s %s：%s从 %s 压回官方 %s"
                       % (where, m.get("id"), zh, format(cur, ","), format(std, ",")))
    if not m.get("cost") and off.get("cost"):
        m["cost"] = off["cost"]
        changes.append("%s %s：补上官方价格" % (where, m.get("id")))
    off_tlm = off.get("thinkingLevelMap")
    if m.get("reasoning") and off_tlm and m.get("thinkingLevelMap") != off_tlm:
        m["thinkingLevelMap"] = off_tlm
        changes.append("%s %s：思考档位按官方修正" % (where, m.get("id")))


def preset_for(api, compat):
    """补齐该协议的中转兼容预设，已有的键不动。"""
    out = dict(compat or {})
    table = ANTHROPIC_PRESET if api == "anthropic-messages" else OPENAI_PRESET
    added = []
    for k, v in table.items():
        if k not in out:
            out[k] = v
            added.append("%s=%s" % (k, json.dumps(v)))
    return out, added


def plan(cfg):
    new = {"providers": {}}
    changes = []
    provs = cfg["providers"]
    merged_away = {b for _a, b in MERGE}

    for name, p in provs.items():
        if name in DROP_PROVIDERS:
            changes.append("删除服务商 %s（%s，已放弃维护的免费聚合站，含 %d 个模型）"
                           % (name, p.get("baseUrl"), len(p.get("models") or [])))
            continue
        if name in merged_away:
            continue                     # 合并到别的条目里，稍后处理

        p = copy.deepcopy(p)
        api = p.get("api")
        base_raw = (p.get("baseUrl") or "").rstrip("/")

        # 1. 地址规范化：OpenAI 要带 /v1，Claude 不能带，Gemini 要带 /v1beta
        want_base = store.base_for_api(api, base_raw)
        if want_base != base_raw:
            changes.append("%s：地址 %s 规范成 %s" % (name, base_raw, want_base))
        p["baseUrl"] = want_base

        # 2. 密钥重复：headers 里硬写的 Authorization 与 apiKey 相同就改用 authHeader
        hdr = p.get("headers") or {}
        auth = hdr.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == p.get("apiKey"):
            hdr = {k: v for k, v in hdr.items() if k != "Authorization"}
            if hdr:
                p["headers"] = hdr
            else:
                p.pop("headers", None)
            p["authHeader"] = True
            changes.append("%s：headers 里重复写的密钥改用 authHeader 自动附加" % name)

        # 3. 兼容预设补齐
        compat, added = preset_for(api, p.get("compat"))
        if added:
            p["compat"] = compat
            changes.append("%s：补上兼容项 %s" % (name, "、".join(added)))
        elif compat:
            p["compat"] = compat

        # 4. 模型逐个处理
        models = []
        for m in p.get("models") or []:
            m = copy.deepcopy(m)
            m_api = m.get("api")
            if m_api and m_api == api:
                m.pop("api", None)
                m.pop("baseUrl", None)
                changes.append("%s %s：去掉与服务商重复的接口格式声明"
                               % (name, m.get("id")))
            elif m_api:
                want = store.base_for_api(m_api, base_raw)
                if m.get("baseUrl") != want:
                    m["baseUrl"] = want
                    changes.append("%s %s：走 %s，补上模型级地址 %s"
                                   % (name, m.get("id"), m_api, want))
                mc, madded = preset_for(m_api, m.get("compat"))
                if madded:
                    m["compat"] = mc
                    changes.append("%s %s：补上该协议的兼容项 %s"
                                   % (name, m.get("id"), "、".join(madded)))
            if m.pop("headers", None) is not None:
                changes.append("%s %s：删掉模型级请求头（pi 只认服务商级的，"
                               "这里还重复存了一份密钥）" % (name, m.get("id")))
            align_model(m, changes, name)
            models.append(m)
        p["models"] = models
        # OpenAI 服务商级 compat 里放错位置的 Claude 专用键，挪走
        if api != "anthropic-messages" and p.get("compat"):
            anth_models = [mm for mm in models
                           if mm.get("api") == "anthropic-messages"]
            for k in list(p["compat"]):
                if k not in ANTHROPIC_ONLY_KEYS:
                    continue
                if anth_models and all(k in (mm.get("compat") or {})
                                       for mm in anth_models):
                    p["compat"].pop(k)
                    changes.append("%s：服务商级的 %s 只对 Claude 协议有效，"
                                   "已挪到对应模型上" % (name, k))
            if not p["compat"]:
                p.pop("compat")
        new["providers"][name] = p

    # 5. 合并同站同密钥的条目
    for keep, gone in MERGE:
        if keep not in new["providers"] or gone not in provs:
            continue
        src = provs[gone]
        dst = new["providers"][keep]
        if (src.get("apiKey") or "") != (dst.get("apiKey") or ""):
            changes.append("%s 与 %s 密钥不同，不合并" % (keep, gone))
            new["providers"][gone] = copy.deepcopy(src)
            continue
        base_raw = (dst.get("baseUrl") or "").rstrip("/")
        src_api = src.get("api")
        for m in src.get("models") or []:
            m = copy.deepcopy(m)
            m["api"] = src_api
            m["baseUrl"] = store.base_for_api(src_api, base_raw)
            mc, _added = preset_for(src_api, m.get("compat"))
            m["compat"] = mc
            if m.pop("headers", None) is not None:
                changes.append("%s %s：删掉模型级请求头（pi 只认服务商级的）"
                               % (keep, m.get("id")))
            align_model(m, changes, keep)
            dst["models"].append(m)
        # 伪装头跟着搬过来（已实测该站的 OpenAI 端点带这套头也正常）
        hdr = dict(dst.get("headers") or {})
        for k, v in (src.get("headers") or {}).items():
            if k.lower() == "authorization":
                continue
            hdr.setdefault(k, v)
        if hdr:
            dst["headers"] = hdr
        if src.get("authHeader") or (src.get("headers") or {}).get("Authorization"):
            dst["authHeader"] = True
        changes.append("把 %s 合并进 %s：%d 个模型改成模型级协议加地址，共用一个条目"
                       % (gone, keep, len(src.get("models") or [])))
    return new, changes


def summary(cfg):
    lines = []
    for name, p in cfg["providers"].items():
        mixed = [m.get("id") for m in p.get("models") or [] if m.get("api")]
        lines.append("  %-12s %-34s %-20s 模型 %d%s"
                     % (name, p.get("baseUrl"), p.get("api"),
                        len(p.get("models") or []),
                        "（其中 %d 个走别的协议：%s）" % (len(mixed), "、".join(mixed))
                        if mixed else ""))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="整理 models.json：规范地址、补官方参数与中转兼容项、"
                    "清理无效字段，可选地删除或合并服务商")
    ap.add_argument("--write", action="store_true", help="真正写盘（默认只演练）")
    ap.add_argument("--drop", action="append", default=[], metavar="服务商",
                    help="删掉这个服务商，可重复")
    ap.add_argument("--merge", action="append", default=[], metavar="保留者=被并入者",
                    help="把后者的模型并进前者（需同站同密钥），可重复")
    args = ap.parse_args()
    DROP_PROVIDERS.extend(args.drop)
    for pair in args.merge:
        if "=" not in pair:
            print("--merge 要写成 保留者=被并入者")
            return 2
        keep, gone = pair.split("=", 1)
        MERGE.append((keep.strip(), gone.strip()))

    path = store.DEFAULT_CONFIG_PATH
    cfg = store.load_config(path)
    new, changes = plan(cfg)

    print("整理前：\n%s\n" % summary(cfg))
    print("整理后：\n%s\n" % summary(new))
    print("共 %d 项改动：" % len(changes))
    for c in changes:
        print("  ·", c)

    errs, warns = validator.check_config(new)
    print("\n自检：%s" % ("通过" if not errs else "不通过"))
    for e in errs:
        print("  错误：", e)
    for w in warns[:6]:
        print("  提醒：", w)
    ok, msg = validator.pi_validate_data(new)
    print("pi 校验器：%s %s" % (ok, msg.splitlines()[0] if msg else ""))
    dry_ok, dry_txt = validator.pi_dry_run(new)
    print("沙盒里让 pi 真读一遍：%s" % dry_ok)
    for line in (dry_txt or "").splitlines():
        low = line.lower()
        if "warning" in low or "error" in low or line.startswith("Provider"):
            print("  ", line)
    if errs or ok is False or dry_ok is False:
        print("\n有问题，不写盘")
        return 1

    if not args.write:
        print("\n这是演练，没有改任何文件。确认后加 --write 执行")
        return 0
    bak = store.write_config(path, new, baseline=cfg)
    print("\n已写入 %s\n备份在 %s" % (path, bak))
    ok2, out = validator.pi_list_models()
    print("\npi 实际读取结果：%s\n%s" % (ok2, out[:2000]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
