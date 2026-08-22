# -*- coding: utf-8 -*-
"""模型实测：可用性、延迟、图片识别、思考能力、上下文抽查。

说明：上下文长度只能抽查（默认约 4000 tokens 输入），
完整标称长度实测要发送几十万 tokens，费用上不现实。

每项测试都会顺带给出可以回写到配置里的结论（结果里的 fixes 字段），
管理窗口可以一键采纳，省去手工改参数。
"""
import base64
import json
import re
import struct
import time
import urllib.parse
import zlib

import fetcher

UA = fetcher.UA

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
VERDICT_ZH = {PASS: "通过", WARN: "警告", FAIL: "失败", SKIP: "跳过"}

DEFAULT_OPTS = {"image": True, "thinking": True, "ctx_tokens": 4000}

# 反探测与限流信号：一旦出现就得停下整轮测试，再撑下去容易被封 IP
_ABORT_RE = re.compile(
    r"bulk\s*model\s*probing|too\s*many\s*requests|rate.?limit|频繁|封禁|"
    r"已被禁|banned|blocked\s*ip|请求过于频密", re.I)


def detect_abort(status, text):
    """返回不该再继续的原因，没有就返回 None。"""
    if status == 429:
        return "服务商返回 429（请求太密）"
    if status and status > 0 and _ABORT_RE.search(text or ""):
        return err_msg(text)
    return None


def response_shape(j):
    """看返回体是哪种协议的格式。

    中转经常出现“标了支持 Claude 接口，实际返回 OpenAI 格式”的情况，
    pi 会直接解析失败，而这种错配光看状态码看不出来。
    """
    if not isinstance(j, dict):
        return None
    if "choices" in j or j.get("object") == "chat.completion":
        return "openai-completions"
    if isinstance(j.get("content"), list) and (
            j.get("type") == "message" or "stop_reason" in j
            or j.get("role") == "assistant"):
        return "anthropic-messages"
    if "candidates" in j:
        return "google-generative-ai"
    return None


API_ZH = {"openai-completions": "OpenAI 兼容",
          "anthropic-messages": "Claude 格式",
          "google-generative-ai": "Gemini 格式"}


def make_png_b64(size=64, rgb=(200, 40, 40)):
    """生成纯色小 PNG 的 base64，用于图片识别测试。"""
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


def err_msg(text):
    """从错误响应里提取人能看懂的信息。"""
    try:
        j = json.loads(text)
        for path in (("error", "message"), ("error",), ("message",), ("msg",)):
            cur = j
            ok = True
            for k in path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, str) and cur:
                return cur[:180]
    except Exception:  # noqa: BLE001
        pass
    return (text or "").strip()[:180] or "空响应"


def _urls_for(api, base, mid, key):
    base = (base or "").strip().rstrip("/")
    versioned = bool(re.search(r"/v\d+[a-z]*$", base))
    if api == "anthropic-messages":
        return [base + "/messages"] if versioned else [base + "/v1/messages"]
    if api == "google-generative-ai":
        b = base if versioned else base + "/v1beta"
        q = "?key=" + urllib.parse.quote(key or "", safe="")
        return [b + "/models/" + mid + ":generateContent" + q]
    if versioned:
        return [base + "/chat/completions"]
    return [base + "/v1/chat/completions", base + "/chat/completions"]


def _headers_for(api, provider):
    key = provider.get("_key") or ""
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    if api == "anthropic-messages":
        h.update({"x-api-key": key, "Authorization": "Bearer %s" % key,
                  "anthropic-version": "2023-06-01"})
    elif api != "google-generative-ai":
        h["Authorization"] = "Bearer %s" % key
    for k, v in (provider.get("headers") or {}).items():
        h[k] = v
    return h


def _post(provider, payload, timeout=45):
    """发一次对话请求，返回 (状态码, 文本, 延迟ms)。自动尝试地址变体。

    payload 里的 _model_id 只读取不删除，方便重试复用。
    """
    api = provider.get("api")
    mid = payload.get("_model_id") or payload.get("model") or "?"
    key = provider.get("_key") or provider.get("apiKey") or ""
    headers = _headers_for(api, provider)
    body = json.dumps({k: v for k, v in payload.items()
                       if k != "_model_id"}, ensure_ascii=False)
    urls = _urls_for(api, provider.get("baseUrl"), mid, key)
    last = (0, "", 0.0)
    for i, url in enumerate(urls):
        t0 = time.time()
        try:
            status, text = fetcher._http_request(url, headers, method="POST",
                                                 body=body, timeout=timeout)
        except fetcher.FetchError as e:
            last = (-1, fetcher.redact(e, key), (time.time() - t0) * 1000)
            continue
        lat = (time.time() - t0) * 1000
        text = fetcher.redact(text, key)
        if status == 200:
            return status, text, lat
        last = (status, text, lat)
        if status in (400, 404) and i < len(urls) - 1:
            continue  # 换地址变体再试
        break
    return last


def _extract(api, j):
    """从成功响应里取 (文本, 思考文本, prompt tokens, completion tokens)。"""
    if api == "anthropic-messages":
        blocks = j.get("content") or []
        txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        th = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
        u = j.get("usage") or {}
        return txt, th, u.get("input_tokens"), u.get("output_tokens")
    if api == "google-generative-ai":
        cands = j.get("candidates") or [{}]
        parts = (cands[0].get("content") or {}).get("parts") or []
        txt = "".join(p.get("text", "") for p in parts)
        u = j.get("usageMetadata") or {}
        return txt, "", u.get("promptTokenCount"), u.get("candidatesTokenCount")
    ch = (j.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    u = j.get("usage") or {}
    return (msg.get("content") or "", msg.get("reasoning_content") or "",
            u.get("prompt_tokens"), u.get("completion_tokens"))


def _base_payload(api, model_id, text, max_tokens):
    """构造三种格式的基本对话请求体，返回 (payload, max_tokens字段名)。"""
    if api == "anthropic-messages":
        return {"model": model_id, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": text}]}, "max_tokens"
    if api == "google-generative-ai":
        return {"contents": [{"parts": [{"text": text}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
                "_model_id": model_id}, "maxOutputTokens"
    return {"model": model_id, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": text}],
            "_model_id": model_id}, "max_tokens"


def _send(provider, payload, timeout, state):
    """发请求，并处理 openai 格式的 max_tokens 字段差异。

    部分中转只认 max_completion_tokens。以前只有第一项测试会重试，
    导致“可用性通过、其余全失败”的误报；现在探测到一次就整轮记住。
    """
    if state.get("maxtok_field") == "max_completion_tokens" \
            and "max_tokens" in payload:
        payload["max_completion_tokens"] = payload.pop("max_tokens")
    status, text, lat = _post(provider, payload, timeout)
    if status == 400 and "max_completion_tokens" in (text or "") \
            and "max_tokens" in payload:
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        state["maxtok_field"] = "max_completion_tokens"
        status, text, lat2 = _post(provider, payload, timeout)
        return status, text, (lat or lat2)
    return status, text, lat


def effective(provider, model):
    """考虑模型级 api 覆盖后的服务商视图，与 pi 的实际行为一致：
    Claude 格式地址不能带 /v1（SDK 自己拼接），OpenAI 要带，Gemini 要带 /v1beta。"""
    api = model.get("api") or provider.get("api")
    base = (provider.get("baseUrl") or "").strip().rstrip("/")
    if api == fetcher.API_ANTHROPIC:
        base = re.sub(r"/v\d+[a-z]*$", "", base)
    p = dict(provider)
    p["api"] = api
    p["baseUrl"] = base
    return p


def _declares_maxtok(provider, model):
    """配置里是否已经明写了最大输出字段（服务商级或模型级）。"""
    for src in (model, provider):
        if (src or {}).get("compat", {}).get("maxTokensField"):
            return True
    return False


def test_model(provider, model, opts=None, stop=None, state=None):
    """测一个模型，返回结果列表。

    provider: models.json 里的服务商对象；model: 其中的一个模型对象。
    opts: {'image': True, 'thinking': True, 'ctx_tokens': 4000 或 0}
    state: 同一轮测试共享的字典（记住 max_tokens 字段等发现）。
    每个结果是 dict：{key, label, verdict, latency, detail, fixes}
    """
    opts = dict(DEFAULT_OPTS, **(opts or {}))
    state = state if state is not None else {}
    api = provider.get("api")
    mid = model.get("id", "?")
    p = dict(provider)
    p["_key"] = provider.get("apiKey") or ""
    results = []

    def add(key, label, verdict, latency=0.0, detail="", fixes=None):
        results.append({"key": key, "label": label, "verdict": verdict,
                        "latency": latency, "detail": detail,
                        "fixes": fixes or []})

    def stopped():
        return stop is not None and stop.is_set()

    if (provider.get("apiKey") or "").startswith(("$", "!")):
        add("key", "密钥", WARN, 0,
            "密钥是环境变量或命令引用，工具按原文发送，测试结果可能不准")

    # 1. 基础对话
    if stopped():
        add("basic", "可用性", SKIP, 0, "已手动停止")
        return results
    payload, _mtf = _base_payload(api, mid, "请只回复一个字：好", 16)
    status, text, lat = _send(p, payload, 45, state)
    stop_reason = detect_abort(status, text)
    if stop_reason:
        state["abort"] = stop_reason
        add("basic", "可用性", FAIL, lat,
            "服务商在限流或反探测，已停下本轮测试：%s" % stop_reason)
        return results
    if status == 200:
        try:
            j = json.loads(text)
        except Exception:  # noqa: BLE001
            j = None
        # 先看协议对不对得上，错配时后面几项测了也没意义
        shape = response_shape(j)
        if shape and shape != api:
            add("basic", "可用性", PASS, lat, "请求能通，但协议对不上，详见下一行")
            add("protocol", "协议匹配", FAIL, lat,
                "这个模型配的是%s，实际返回的是%s的格式，pi 会解析失败，"
                "建议改成%s" % (API_ZH.get(api, api), API_ZH.get(shape, shape),
                                 API_ZH.get(shape, shape)),
                [{"kind": "api", "value": shape,
                  "why": "实测返回的是%s的格式" % API_ZH.get(shape, shape)}])
            return results
        try:
            txt, th, _pt, _ct = _extract(api, j or {})
        except Exception:  # noqa: BLE001
            txt, th = "", ""
        fixes = []
        if state.get("maxtok_field") == "max_completion_tokens":
            fixes.append({"kind": "maxTokensField",
                          "value": "max_completion_tokens",
                          "why": "该中转只认 max_completion_tokens"})
        elif api == "openai-completions" and not _declares_maxtok(provider, model):
            # pi 对不在白名单里的中转默认发 max_completion_tokens，
            # 实测认 max_tokens 就得把这一条明写进配置，否则 pi 里仍会报错
            fixes.append({"kind": "maxTokensField", "value": "max_tokens",
                          "why": "实测认 max_tokens，而 pi 对未知中转默认发 max_completion_tokens"})
        if (txt or th).strip():
            add("basic", "可用性", PASS, lat,
                "返回正常：%s" % (txt or th).strip()[:30], fixes)
        else:
            add("basic", "可用性", WARN, lat, "请求成功但没返回内容", fixes)
    elif status == -1:
        add("basic", "可用性", FAIL, lat, "连不上服务器：%s" % err_msg(text))
        return results
    else:
        add("basic", "可用性", FAIL, lat, "HTTP %s：%s" % (status, err_msg(text)))
        return results  # 基础都失败，后面没必要测

    def check_abort(st, tx):
        """中途撞上限流或反探测就标记下来，让整轮测试停住。"""
        why = detect_abort(st, tx)
        if why:
            state["abort"] = why
        return why

    # 2. 图片识别
    conf_image = "image" in (model.get("input") or [])
    if not opts.get("image"):
        add("image", "图片识别", SKIP, 0, "本轮没勾选图片测试")
    elif stopped():
        add("image", "图片识别", SKIP, 0, "已手动停止")
        return results
    else:
        b64 = make_png_b64()
        q = "这张图片主要是什么颜色？只回答一个颜色词。"
        if api == "anthropic-messages":
            content = [{"type": "image", "source": {"type": "base64",
                       "media_type": "image/png", "data": b64}},
                       {"type": "text", "text": q}]
            payload = {"model": mid, "max_tokens": 32, "messages":
                       [{"role": "user", "content": content}]}
        elif api == "google-generative-ai":
            payload = {"contents": [{"parts": [
                {"inline_data": {"mime_type": "image/png", "data": b64}},
                {"text": q}]}],
                "generationConfig": {"maxOutputTokens": 32},
                "_model_id": mid}
        else:
            payload = {"model": mid, "max_tokens": 32, "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": q},
                            {"type": "image_url",
                             "image_url": {"url": "data:image/png;base64," + b64}}]}],
                "_model_id": mid}
        status, text, lat = _send(p, payload, 60, state)
        if check_abort(status, text):
            add("image", "图片识别", SKIP, lat, "服务商限流，已停下本轮测试")
            return results
        if status == 200:
            try:
                txt, _th, _pt, _ct = _extract(api, json.loads(text))
            except Exception:  # noqa: BLE001
                txt = ""
            note = ""
            fixes = []
            if txt and ("红" in txt or "red" in txt.lower()):
                note = "，识别正确"
            if not conf_image:
                note += "（配置里没开图片，实测却支持，可以开启）"
                fixes.append({"kind": "input_image", "value": True,
                              "why": "实测支持图片输入"})
            add("image", "图片识别", PASS, lat, "支持图片输入%s，回答：%s"
                % (note, (txt or "").strip()[:20] or "空"), fixes)
        else:
            msg = err_msg(text)
            if re.search(r"image|vision|multimodal|not.?support|不支持", msg, re.I):
                if conf_image:
                    add("image", "图片识别", FAIL, lat,
                        "配置声明支持图片但实测被拒，建议关掉图片：" + msg,
                        [{"kind": "input_image", "value": False,
                          "why": "实测被拒绝，配置里却开着图片"}])
                else:
                    add("image", "图片识别", SKIP, lat,
                        "纯文字模型拒绝图片，与配置一致")
            else:
                add("image", "图片识别", WARN, lat, "图片请求失败（可能不支持）：" + msg)

    # 3. 思考能力
    if not opts.get("thinking"):
        add("think", "思考能力", SKIP, 0, "本轮没勾选思考测试")
    elif not model.get("reasoning"):
        add("think", "思考能力", SKIP, 0, "配置为不支持思考，跳过")
    elif stopped():
        add("think", "思考能力", SKIP, 0, "已手动停止")
        return results
    else:
        if api == "anthropic-messages":
            payload = {"model": mid, "max_tokens": 2048,
                       "thinking": {"type": "enabled", "budget_tokens": 1024},
                       "messages": [{"role": "user",
                                     "content": "9.11 和 9.9 哪个大？简答"}]}
        elif api == "google-generative-ai":
            payload = {"contents": [{"parts": [
                {"text": "9.11 和 9.9 哪个大？简答"}]}],
                "generationConfig": {"thinkingConfig": {"thinkingBudget": 512}},
                "_model_id": mid}
        else:
            payload = {"model": mid, "max_tokens": 512,
                       "reasoning_effort": "low",
                       "messages": [{"role": "user",
                                     "content": "9.11 和 9.9 哪个大？简答"}],
                       "_model_id": mid}
        status, text, lat = _send(p, payload, 60, state)
        if check_abort(status, text):
            add("think", "思考能力", SKIP, lat, "服务商限流，已停下本轮测试")
            return results
        if status == 200:
            try:
                txt, th, _pt, _ct = _extract(api, json.loads(text))
            except Exception:  # noqa: BLE001
                txt, th = "", ""
            if th.strip():
                add("think", "思考能力", PASS, lat, "接受思考参数，返回了思考内容")
            elif (txt or "").strip():
                add("think", "思考能力", WARN, lat,
                    "接受思考参数，但未见思考内容（可能不外显）")
            else:
                add("think", "思考能力", WARN, lat, "请求成功但没返回内容")
        else:
            msg = err_msg(text)
            fixes = []
            if re.search(r"reasoning|thinking|思考", msg, re.I):
                fixes.append({"kind": "reasoning", "value": False,
                              "why": "实测明确拒绝思考参数"})
            add("think", "思考能力", WARN, lat,
                "思考参数被拒绝（该中转可能用别的思考格式）：%s" % msg, fixes)

    # 4. 上下文抽查
    n = int(opts.get("ctx_tokens") or 0)
    if n <= 0:
        add("ctx", "上下文抽查", SKIP, 0, "未启用")
    elif stopped():
        add("ctx", "上下文抽查", SKIP, 0, "已手动停止")
    else:
        words = max(50, int(n * 0.75))
        filler = "apple orange mango grape lemon " * (words // 5 + 1)
        filler = filler[: int(len(filler) * 0.98)]
        prompt = ("下面的填充文本仅用于测试，请只回复两个字：收到\n填充：%s" % filler)
        payload, _mt = _base_payload(api, mid, prompt, 16)
        status, text, lat = _send(p, payload, 120, state)
        if check_abort(status, text):
            add("ctx", "上下文抽查", SKIP, lat, "服务商限流，已停下本轮测试")
            return results
        if status == 200:
            try:
                _txt, _th, pt, _ct = _extract(api, json.loads(text))
            except Exception:  # noqa: BLE001
                pt = None
            if pt:
                add("ctx", "上下文抽查", PASS, lat,
                    "抽查通过，服务端计费 %s 个输入 tokens（目标约 %s，不代表标称全长）"
                    % ("{:,}".format(pt), "{:,}".format(n)))
            else:
                add("ctx", "上下文抽查", PASS, lat,
                    "抽查约 %s tokens 输入通过（不代表标称全长）"
                    % "{:,}".format(n))
        else:
            msg = err_msg(text)
            if re.search(r"context|length|too.?many|token|maximum|超长|过长",
                         msg, re.I):
                add("ctx", "上下文抽查", FAIL, lat,
                    "约 %s tokens 就被拒，标称上下文可能虚标：%s"
                    % ("{:,}".format(n), msg))
            else:
                add("ctx", "上下文抽查", WARN, lat, "抽查失败：" + msg)
    return results


def collect_fixes(results):
    """把一组结果里的可回写结论汇总（同一项以后出现的为准）。"""
    out = {}
    for r in results or []:
        for fx in r.get("fixes") or []:
            out[fx.get("kind")] = fx
    return list(out.values())


def fix_text(fix):
    kind = fix.get("kind")
    val = fix.get("value")
    why = fix.get("why") or ""
    label = {"input_image": "图片输入", "reasoning": "思考支持",
             "maxTokensField": "最大输出字段",
             "contextWindow": "上下文长度",
             "api": "接口格式"}.get(kind, kind)
    if kind in ("input_image", "reasoning"):
        val = "支持" if val else "不支持"
    elif kind == "api":
        val = API_ZH.get(val, val)
    return "%s 改为 %s（%s）" % (label, val, why) if why \
        else "%s 改为 %s" % (label, val)


def overall_of(results):
    """给一个模型的整组结果下结论：基础对话失败或协议错配即不可用；
    其余有失败或警告给警告；跳过不参与。"""
    fatal = [r for r in results if r["key"] in ("basic", "protocol")]
    if any(r["verdict"] == FAIL for r in fatal):
        return FAIL
    counted = [r for r in results if r["key"] not in ("basic", "protocol")]
    if any(r["verdict"] == FAIL for r in counted):
        return WARN
    if any(r["verdict"] == WARN for r in counted):
        return WARN
    return PASS
