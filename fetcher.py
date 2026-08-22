# -*- coding: utf-8 -*-
"""从第三方服务商拉取模型列表（标准库实现，无第三方依赖）。

支持三种接口格式，自动模式下按 OpenAI 兼容、Claude、Gemini 的顺序尝试，
哪种先成功就用哪种。

密钥安全：借 Node 发请求时，请求内容走标准输入传给子进程，
不放在命令行参数里（Windows 上同一用户的其他进程能看到别人的命令行）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import ssl
import urllib.request
import urllib.error

TIMEOUT = 25
SSL_CTX = ssl.create_default_context()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) pi-model-config-tool/2.0")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 有些中转前面有 Cloudflare 指纹拦截，只放行 Node 客户端。
# 本机装了 Node 就优先借它发请求，和 pi 同引擎，兼容性最好；
# 没有 Node（或找不到桥接脚本）时退回 Python 直接请求。
_NODE_PATH = shutil.which("node")
_SCRIPT_FILE = -1  # -1 表示还没找过


def _script_path():
    global _SCRIPT_FILE
    if _SCRIPT_FILE == -1:
        _SCRIPT_FILE = None
        cands = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(os.path.join(meipass, "nodefetch.mjs"))
        cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "nodefetch.mjs"))
        for p in cands:
            if os.path.isfile(p):
                _SCRIPT_FILE = p
                break
    return _SCRIPT_FILE


API_AUTO = "auto"
API_OPENAI = "openai-completions"
API_ANTHROPIC = "anthropic-messages"
API_GOOGLE = "google-generative-ai"

API_LABELS = {
    "自动检测": API_AUTO,
    "OpenAI 兼容": API_OPENAI,
    "Claude 格式": API_ANTHROPIC,
    "Gemini 格式": API_GOOGLE,
}
LABEL_BY_API = {v: k for k, v in API_LABELS.items()}


class FetchError(Exception):
    pass


def redact(text, *secrets):
    """把密钥从要展示的文字里抹掉（原文和转义后的写法都抹）。"""
    s = "" if text is None else str(text)
    for sec in secrets:
        sec = (sec or "").strip()
        if len(sec) < 6:
            continue
        for form in (sec, urllib.request.quote(sec),
                     urllib.request.quote(sec, safe="")):
            s = s.replace(form, "***")
    return s


def _node_request(url, headers, method, body, timeout):
    """借 Node 发请求。返回 (状态码, 文本)，不可用时返回 None。"""
    script = _script_path()
    if not (_NODE_PATH and script):
        return None
    payload = json.dumps({"url": url, "headers": headers, "method": method,
                          "body": body, "timeout": timeout})
    try:
        r = subprocess.run([_NODE_PATH, script], input=payload,
                           capture_output=True, text=True,
                           timeout=timeout + 15, encoding="utf-8",
                           errors="replace", creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout or ""
    if not out.startswith("@@STATUS@@"):
        return None
    nl = out.find("\n")
    if nl < 0:
        return None
    try:
        status = int(out[len("@@STATUS@@"):nl].strip() or "-1")
    except ValueError:
        return None
    return status, out[nl + 1:]


def _http_request(url, headers, method="GET", body=None, timeout=TIMEOUT):
    """返回 (状态码, 响应文本)。网络层连不上抛 FetchError。

    状态码 -1 表示请求没发出去或被中断，文本是原因。
    """
    got = _node_request(url, headers, method, body, timeout)
    if got is not None:
        return got
    req = urllib.request.Request(url, headers=headers, method=method,
                                 data=body.encode("utf-8") if body else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return e.code, body_txt
    except urllib.error.URLError as e:
        raise FetchError("连不上服务器：%s" % (e.reason,)) from e
    except (ssl.SSLError, OSError) as e:
        raise FetchError("连不上服务器：%s" % (e,)) from e


def _get_json(url, headers):
    status, text = _http_request(url, headers)
    if status == -1:
        raise FetchError("连接失败：%s" % text[:150])
    if status != 200:
        raise FetchError("服务器返回 %s，响应片段：%s" % (status, text.strip()[:200]))
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise FetchError("返回内容不是有效的 JSON") from e


def _url_variants(base_url, kind):
    base = (base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise FetchError("地址要以 http:// 或 https:// 开头")
    versioned = bool(re.search(r"/v\d+[a-z]*$", base))
    if kind == API_GOOGLE:
        if versioned:
            return [base + "/models"]
        return [base + "/v1beta/models", base + "/models"]
    if versioned:
        return [base + "/models"]
    return [base + "/v1/models", base + "/models"]


def _parse_openai(data):
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.get("models", [])
    ids = []
    for it in items or []:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name")
            if mid:
                ids.append(mid)
    return ids


def _parse_google(data):
    ids = []
    for m in (data.get("models") or []) if isinstance(data, dict) else []:
        name = m.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        if name:
            ids.append(name)
    return ids


def fetch_models(base_url, api_key, api_type=API_AUTO):
    """返回 (实际接口类型, 模型ID列表, 实际生效的基础地址)。失败抛 FetchError。"""
    if api_type in (API_OPENAI, API_ANTHROPIC, API_GOOGLE):
        return _try_one(base_url, api_key, api_type)
    errs = []
    for t in (API_OPENAI, API_ANTHROPIC, API_GOOGLE):
        try:
            return _try_one(base_url, api_key, t)
        except FetchError as e:
            errs.append("%s：%s" % (LABEL_BY_API[t], e))
    raise FetchError("三种格式都拉取失败。" + "；".join(errs))


def _try_one(base_url, api_key, kind):
    last_err = None
    for url in _url_variants(base_url, kind):
        eff_base = re.sub(r"/models$", "", url)
        if kind == API_OPENAI:
            headers = {"Authorization": "Bearer %s" % api_key}
        elif kind == API_ANTHROPIC:
            headers = {
                "x-api-key": api_key,
                "Authorization": "Bearer %s" % api_key,
                "anthropic-version": "2023-06-01",
            }
        else:
            sep = "&" if "?" in url else "?"
            headers = {}
            url = url + sep + "key=" + urllib.request.quote(api_key or "", safe="")
        headers.setdefault("User-Agent", UA)
        try:
            data = _get_json(url, headers)
            ids = _parse_google(data) if kind == API_GOOGLE else _parse_openai(data)
            if not ids:
                last_err = FetchError("列表为空或格式不认识")
                continue
            seen, uniq = set(), []
            for i in ids:
                if i not in seen:
                    seen.add(i)
                    uniq.append(i)
            return kind, uniq, re.sub(r"[?&]key=[^&]*$", "", eff_base)
        except FetchError as e:
            last_err = FetchError(redact(e, api_key))
    raise last_err or FetchError("拉取失败")
