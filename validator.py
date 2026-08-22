# -*- coding: utf-8 -*-
"""写入前的配置自检，以及借 pi 本体做的复核。

为什么必须有这一层：pi 读 models.json 用严格 schema 校验，
**任何一处字段非法都会让整份配置作废，所有服务商一起消失**（包括工具没碰过的）。
所以写盘前先自己查一遍，本机装了 pi 再让 pi 自己的校验器把关。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import catalog as catalog_mod

LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
INPUT_KINDS = ("text", "image")
COST_RATES = ("input", "output", "cacheRead", "cacheWrite")

# 这些键在 pi 的 compat schema 里有明确类型，写错了容易出问题（只提醒，不拦截）
_COMPAT_BOOL = {
    "supportsStore", "supportsDeveloperRole", "supportsReasoningEffort",
    "supportsUsageInStreaming", "requiresToolResultName",
    "requiresAssistantAfterToolResult", "requiresThinkingAsText",
    "requiresReasoningContentOnAssistantMessages", "supportsOpenAIGrammarTools",
    "supportsStrictMode", "sendSessionAffinityHeaders",
    "supportsLongCacheRetention", "supportsEagerToolInputStreaming",
    "supportsCacheControlOnTools", "supportsTemperature",
    "forceAdaptiveThinking", "allowEmptySignature", "supportsStrictTools",
    "supportsToolReferences", "supportsAdditionalTools", "supportsToolSearch",
}
_MAXTOK_FIELDS = ("max_tokens", "max_completion_tokens")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and len(v) >= 1


def _check_str_map(obj, where, errors, what="请求头"):
    if not isinstance(obj, dict):
        errors.append("%s：%s 必须是对象" % (where, what))
        return
    for k, v in obj.items():
        if not isinstance(k, str) or not isinstance(v, str):
            errors.append("%s：%s 的键和值都必须是文本（问题项 %r）" % (where, what, k))


def _check_cost(cost, where, errors, warnings):
    if not isinstance(cost, dict):
        errors.append("%s：cost 必须是对象" % where)
        return
    missing = [k for k in COST_RATES if k not in cost]
    if missing:
        errors.append("%s：cost 缺少 %s（pi 要求四项价格齐全，缺一项整份配置都会被拒）"
                      % (where, "、".join(missing)))
    for k in COST_RATES:
        if k in cost and not _is_num(cost[k]):
            errors.append("%s：cost.%s 必须是数字" % (where, k))
    tiers = cost.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, list):
            errors.append("%s：cost.tiers 必须是数组" % where)
        else:
            for i, t in enumerate(tiers):
                w2 = "%s cost.tiers[%d]" % (where, i)
                if not isinstance(t, dict):
                    errors.append("%s：必须是对象" % w2)
                    continue
                if not _is_num(t.get("inputTokensAbove")):
                    errors.append("%s：缺少 inputTokensAbove 或不是数字" % w2)
                for k in COST_RATES:
                    if not _is_num(t.get(k)):
                        errors.append("%s：缺少 %s 或不是数字" % (w2, k))
    extra = [k for k in cost if k not in COST_RATES and k != "tiers"]
    if extra:
        warnings.append("%s：cost 里有 pi 不认识的键 %s" % (where, "、".join(extra)))


def _check_compat(compat, where, warnings):
    if not isinstance(compat, dict):
        warnings.append("%s：compat 不是对象，pi 可能忽略它" % where)
        return
    for k, v in compat.items():
        if k in _COMPAT_BOOL and not isinstance(v, bool):
            warnings.append("%s：compat.%s 一般是 true 或 false，当前是 %r"
                            % (where, k, v))
        if k == "maxTokensField" and v not in _MAXTOK_FIELDS:
            warnings.append("%s：compat.maxTokensField 只能是 %s"
                            % (where, " 或 ".join(_MAXTOK_FIELDS)))


def _check_model(m, where, errors, warnings):
    if not isinstance(m, dict):
        errors.append("%s：模型必须是对象" % where)
        return
    if not _nonempty_str(m.get("id")):
        errors.append("%s：模型缺少 id（不能为空）" % where)
    for key in ("name", "api", "baseUrl"):
        if key in m and not _nonempty_str(m[key]):
            errors.append("%s：%s 不能是空文本" % (where, key))
    if "reasoning" in m and not isinstance(m["reasoning"], bool):
        errors.append("%s：reasoning 必须是 true 或 false" % where)
    tlm = m.get("thinkingLevelMap")
    if tlm is not None:
        if not isinstance(tlm, dict):
            errors.append("%s：thinkingLevelMap 必须是对象" % where)
        else:
            for k, v in tlm.items():
                if k not in LEVELS:
                    warnings.append("%s：thinkingLevelMap 里有 pi 不认识的档位 %r"
                                    % (where, k))
                elif not (v is None or isinstance(v, str)):
                    errors.append("%s：thinkingLevelMap.%s 只能是文本或 null" % (where, k))
    inp = m.get("input")
    if inp is not None:
        if not isinstance(inp, list):
            errors.append("%s：input 必须是数组" % where)
        else:
            bad = [x for x in inp if x not in INPUT_KINDS]
            if bad:
                errors.append("%s：input 只能包含 text 和 image（问题值 %s）"
                              % (where, "、".join(map(repr, bad))))
    for key in ("contextWindow", "maxTokens"):
        if key in m:
            if not _is_num(m[key]):
                errors.append("%s：%s 必须是数字" % (where, key))
            elif m[key] <= 0:
                warnings.append("%s：%s 是 %s，pi 会当成没填并套用默认值"
                                % (where, key, m[key]))
    if "cost" in m:
        _check_cost(m["cost"], where, errors, warnings)
    if "headers" in m:
        _check_str_map(m["headers"], where, errors, "模型级请求头")
    if "compat" in m:
        _check_compat(m["compat"], where, warnings)
    if "samplingParams" in m and not isinstance(m["samplingParams"], dict):
        errors.append("%s：samplingParams 必须是对象" % where)


def check_config(data):
    """按 pi 的 schema 自查一份完整配置，返回 (致命问题, 提醒)。"""
    errors, warnings = [], []
    if not isinstance(data, dict):
        return ["配置顶层必须是对象"], []
    provs = data.get("providers")
    if not isinstance(provs, dict):
        return ["配置顶层缺少 providers 对象"], []
    for pname, p in provs.items():
        where = "服务商 %s" % pname
        if not isinstance(pname, str) or not pname:
            errors.append("有一个服务商的名字为空")
        if not isinstance(p, dict):
            errors.append("%s：内容必须是对象" % where)
            continue
        for key, label in (("name", "name"), ("baseUrl", "baseUrl"),
                           ("api", "api")):
            if key in p and not _nonempty_str(p[key]):
                errors.append("%s：%s 不能是空文本" % (where, label))
        if "apiKey" in p and not _nonempty_str(p["apiKey"]):
            errors.append("%s：密钥是空的。pi 要求密钥至少 1 个字符，"
                          "写空会让整份 models.json 作废，所有服务商都用不了。"
                          "要留空请直接不写这个字段。" % where)
        if not p.get("apiKey") and "apiKey" not in p and not p.get("oauth"):
            warnings.append("%s：没有密钥，pi 里这个服务商会因为缺密钥而不可用"
                            % where)
        if "oauth" in p and p["oauth"] != "radius":
            errors.append("%s：oauth 只能是 radius" % where)
        if "authHeader" in p and not isinstance(p["authHeader"], bool):
            errors.append("%s：authHeader 必须是 true 或 false" % where)
        if "headers" in p:
            _check_str_map(p["headers"], where, errors)
            for k, v in (p.get("headers") or {}).items():
                if isinstance(v, str) and v.strip() in ("...", "…", ""):
                    warnings.append("%s：请求头 %s 的值看着像没填完的示例（%r）"
                                    % (where, k, v))
        if "compat" in p:
            _check_compat(p["compat"], where, warnings)
        models = p.get("models")
        if models is not None:
            if not isinstance(models, list):
                errors.append("%s：models 必须是数组" % where)
            else:
                ids = []
                for i, m in enumerate(models):
                    _check_model(m, "%s 第 %d 个模型" % (where, i + 1),
                                 errors, warnings)
                    if isinstance(m, dict) and m.get("id"):
                        ids.append(m["id"])
                dup = sorted({x for x in ids if ids.count(x) > 1})
                if dup:
                    warnings.append("%s：模型 ID 重复 %s，pi 只会用第一个"
                                    % (where, "、".join(dup)))
        ov = p.get("modelOverrides")
        if ov is not None:
            if not isinstance(ov, dict):
                errors.append("%s：modelOverrides 必须是对象" % where)
            else:
                for mid, m in ov.items():
                    o = dict(m) if isinstance(m, dict) else m
                    if isinstance(o, dict):
                        o.setdefault("id", mid or "x")
                    _check_model(o, "%s 的覆盖项 %s" % (where, mid),
                                 errors, warnings)
    return errors, warnings


# ---------- 借 pi 本体复核 ----------

def _script_path(name):
    cands = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, name))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
    for p in cands:
        if os.path.isfile(p):
            return p
    return None


def pi_available():
    """本机能不能借 pi 复核（要同时有 node、pi 安装目录和校验脚本）。"""
    return bool(shutil.which("node") and catalog_mod.find_pi_dist()
                and _script_path("pivalidate.mjs"))


def pi_validate_data(data, timeout=40):
    """把配置写到临时文件，交给 pi 自己的校验器验一遍。

    返回 (状态, 说明)。状态 True 通过，False 不通过，None 表示没法验。
    """
    node = shutil.which("node")
    dist = catalog_mod.find_pi_dist()
    script = _script_path("pivalidate.mjs")
    if not (node and dist and script):
        return None, "本机没找到 node 或 pi 安装目录，跳过 pi 复核"
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="pimct_check_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            r = subprocess.run([node, script, dist, tmp], capture_output=True,
                               text=True, timeout=timeout, encoding="utf-8",
                               errors="replace", creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, "调用 pi 校验器失败：%s" % e
        out = (r.stdout or "").strip().splitlines()
        if not out:
            return None, "pi 校验器没有输出：%s" % (r.stderr or "")[:200]
        try:
            j = json.loads(out[-1])
        except ValueError:
            return None, "pi 校验器输出看不懂：%s" % out[-1][:200]
        if j.get("ok") is True:
            return True, "pi 校验通过，可识别服务商：%s" % "、".join(
                j.get("providers") or [])
        if j.get("ok") is False:
            return False, j.get("error") or "pi 拒绝了这份配置"
        return None, j.get("error") or "无法调用 pi 校验器"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def pi_dry_run(data, timeout=90):
    """在沙盒里让真正的 pi 读一遍候选配置。

    schema 校验只管 JSON 结构，pi 组装模型时还有一层检查（缺 api、缺 baseUrl 等），
    那一层出错会让对应服务商整个被丢掉。这里把配置写进临时目录、
    临时把子进程的家目录指过去，跑一次 pi --list-models，看 pi 真实的反应。
    完全不碰用户真正的 models.json。

    返回 (是否没有警告, 输出文本)。没装 pi 时返回 (None, 说明)。
    """
    pi = shutil.which("pi")
    if not pi:
        return None, "本机没找到 pi 命令，跳过沙盒预演"
    home = tempfile.mkdtemp(prefix="pimct_dry_")
    try:
        agent = os.path.join(home, ".pi", "agent")
        os.makedirs(agent, exist_ok=True)
        with open(os.path.join(agent, "models.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        env = dict(os.environ)
        env["USERPROFILE"] = home
        env["HOME"] = home
        env.pop("HOMEDRIVE", None)
        env["HOMEPATH"] = home
        try:
            r = subprocess.run([pi, "--list-models"], capture_output=True,
                               text=True, timeout=timeout, encoding="utf-8",
                               errors="replace", creationflags=_NO_WINDOW,
                               env=env, cwd=home)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, "沙盒预演调用 pi 失败：%s" % e
        text = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
        bad = ("error" in text.lower() or "warning" in text.lower()
               or r.returncode != 0)
        return (not bad), text or "（pi 没有输出）"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def pi_list_models(provider_name=None, timeout=60):
    """跑 pi --list-models，看 pi 实际认到了哪些模型。

    返回 (是否成功, 输出文本)。没装 pi 时返回 (None, 说明)。
    """
    pi = shutil.which("pi")
    if not pi:
        return None, "本机没找到 pi 命令，跳过复核"
    cmd = [pi, "--list-models"]
    if provider_name:
        cmd.append(provider_name)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace",
                           creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "调用 pi 失败：%s" % e
    text = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
    return r.returncode == 0, text or "（pi 没有输出）"
