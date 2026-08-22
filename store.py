# -*- coding: utf-8 -*-
"""配置生成、备份与合并写入。

写入原则：只整体替换用户正在配置的那一个服务商，其他服务商原样保留；
每次写入前先做带时间戳的备份，并且**写盘前一定过一遍自检**
（pi 只要发现一处非法字段就会丢掉整份配置）。
"""
import glob
import json
import os
import shutil
import re
import time

import validator

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".pi", "agent", "models.json"
)

# 这些兼容项只对官方网关的路由有意义，第三方中转用不上，照抄反而碍事
COMPAT_STRIP = {"openRouterRouting", "vercelGatewayRouting"}

LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
LEVEL_ZH = {"off": "关", "minimal": "极低", "low": "低", "medium": "中",
            "high": "高", "xhigh": "超高", "max": "最大"}

# 服务商条目里由本工具负责生成的键，其余键在刷新写入时原样保留
PROVIDER_MANAGED = {"baseUrl", "api", "apiKey", "authHeader", "headers", "models"}
# 模型条目里由本工具负责生成的键，其余键原样透传
# baseUrl 也归工具管：模型单独改了接口格式时要跟着改地址，旧值不能留着
MODEL_MANAGED = {"id", "name", "api", "baseUrl", "reasoning", "thinkingLevelMap",
                 "input", "contextWindow", "maxTokens"}

# 第三方中转的兼容预设。
# 为什么默认要写：pi-ai 对这两项用的是白名单判断，
# 不在名单里的中转会被当成标准 OpenAI：发 developer 角色、
# 发 max_completion_tokens，而大多数中转只认 user 角色和 max_tokens。
RELAY_COMPAT_OPENAI = {
    "no_developer_role": ("supportsDeveloperRole", False),
    "max_tokens_field": ("maxTokensField", "max_tokens"),
    "tolerate_no_finish_reason": ("supportsFinishReason", False),
}
RELAY_COMPAT_ANTHROPIC = {
    "no_eager_tool_streaming": ("supportsEagerToolInputStreaming", False),
    "allow_empty_signature": ("allowEmptySignature", True),
}
# 默认勾上的预设项
RELAY_COMPAT_DEFAULT_ON = {"no_developer_role", "max_tokens_field",
                           "no_eager_tool_streaming"}
# supportsFinishReason 没写进 pi 的 models.json schema，但 pi-ai 运行时确实读它
RELAY_COMPAT_UNDOCUMENTED = {"tolerate_no_finish_reason"}

RELAY_COMPAT_LABEL = {
    "no_developer_role": "关掉 developer 角色（多数中转不认）",
    "max_tokens_field": "最大输出字段用 max_tokens（pi 对未知中转默认发 max_completion_tokens）",
    "tolerate_no_finish_reason": "容忍缺少 finish_reason（聚合站常见，未公开字段）",
    "no_eager_tool_streaming": "关掉工具输入流式（Claude 格式中转建议关）",
    "allow_empty_signature": "允许无签名的思考块（部分中转需要）",
}


def base_for_api(api, base_url):
    """把地址改成某种接口格式要的形式。

    pi 对三种格式的要求不同：OpenAI 要带 /v1；
    Claude 不能带（SDK 自己拼 /v1/messages）；Gemini 要带 /v1beta。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return base
    versioned = bool(re.search(r"/v\d+[a-z]*$", base))
    if api == "anthropic-messages":
        return re.sub(r"/v\d+[a-z]*$", "", base)
    if api == "google-generative-ai":
        return base if versioned else base + "/v1beta"
    return base if versioned else base + "/v1"


def relay_compat(api, options):
    """根据勾选情况生成中转兼容预设。options 是 {预设名: 是否勾选}。"""
    table = RELAY_COMPAT_ANTHROPIC if api == "anthropic-messages" \
        else RELAY_COMPAT_OPENAI
    out = {}
    for key, (field, value) in table.items():
        if (options or {}).get(key):
            out[field] = value
    return out


def relay_compat_state(api, compat):
    """反向读取：从已有 compat 里看出哪些预设已经生效（导入时回填界面）。

    两种协议的预设一起看，没有证据的一律算未勾选，
    这样“导入已配置再写入”不会偷偷改动人家原来的 compat。
    api 参数保留只为语义清晰，判断时不依赖它。
    """
    compat = compat or {}
    out = {}
    for table in (RELAY_COMPAT_OPENAI, RELAY_COMPAT_ANTHROPIC):
        for key, (field, value) in table.items():
            out[key] = compat.get(field) == value
    return out

# 请求头里这些值一眼就是没填完的示例，不该写进配置
HEADER_PLACEHOLDERS = {"", "...", "…", "..", "。。。"}

BACKUP_KEEP = 10


class ModelRow:
    """界面里一行模型的所有状态。"""

    def __init__(self, mid):
        self.mid = mid
        self.checked = False
        self.matched = None            # CatalogEntry 或 None
        self.score = 0.0
        # 以下为可编辑字段（匹配成功时用官方值初始化）
        self.name = ""
        self.reasoning = False
        self.input_image = False
        self.context_window = 0
        self.max_tokens = 0
        # 模型级接口格式覆盖：None=跟随服务商；否则为三种 api 之一
        self.api_override = None
        # 是否来自已保存的配置（刷新已有服务商时为 True）
        self.from_config = False
        # 原始配置字典（来自已保存配置时保留，重写时透传 cost、compat、headers 等）
        self.raw = None
        # 思考等级映射：{level: "keep"|"yes"|"no"}，keep 表示不写这个键
        self.tlm = {lv: "keep" for lv in LEVELS}

    def apply_template(self, entry, score):
        self.matched = entry
        self.score = score
        self.raw = None
        m = entry.model
        self.name = m.get("name") or m.get("id") or self.mid
        self.reasoning = bool(m.get("reasoning"))
        self.input_image = "image" in (m.get("input") or [])
        self.context_window = int(m.get("contextWindow") or 0)
        self.max_tokens = int(m.get("maxTokens") or 0)
        tlm = m.get("thinkingLevelMap") or {}
        for lv in LEVELS:
            if lv in tlm:
                self.tlm[lv] = "no" if tlm[lv] is None else "yes"
            else:
                self.tlm[lv] = "keep"

    def apply_manual(self):
        """未识别时的手工默认值。"""
        self.matched = None
        self.score = 0.0
        self.raw = None
        self.name = self.mid
        self.reasoning = False
        self.input_image = False
        self.context_window = 128000
        self.max_tokens = 16384

    def explicit_tlm(self):
        """只含明确表态档位的映射，供界面显示用（keep 的档位不放进来）。"""
        out = {}
        for lv in LEVELS:
            st = self.tlm.get(lv, "keep")
            if st == "yes":
                out[lv] = lv
            elif st == "no":
                out[lv] = None
        return out


def row_from_config(m):
    """从 models.json 的模型对象构建界面行（刷新已有服务商时用）。"""
    row = ModelRow(m.get("id", "?"))
    row.from_config = True
    row.apply_manual()
    row.name = m.get("name") or row.mid
    row.reasoning = bool(m.get("reasoning"))
    row.input_image = "image" in (m.get("input") or [])
    try:
        row.context_window = int(m.get("contextWindow") or 0)
        row.max_tokens = int(m.get("maxTokens") or 0)
    except (TypeError, ValueError):
        pass
    tlm = m.get("thinkingLevelMap") or {}
    for lv in LEVELS:
        if lv in tlm:
            row.tlm[lv] = "no" if tlm[lv] is None else "yes"
        else:
            row.tlm[lv] = "keep"
    row.api_override = m.get("api") or None
    row.raw = m
    return row


def build_model_dict(row, provider_api=None, provider_base=None,
                     relay_options=None):
    """生成一个模型条目。

    模型单独指定了接口格式、且与服务商不同时，会同时写模型级 baseUrl，
    这样一个服务商里就能混用两种协议，不必拆成两个条目
    （pi 取值时模型级 baseUrl 优先于服务商级）。
    """
    if not row.mid:
        raise ValueError("模型 ID 不能为空")
    # 来自已保存配置的行，把工具不管理的字段（cost、compat、headers 等）原样透传
    e = {}
    if getattr(row, "raw", None):
        for k, v in row.raw.items():
            if k not in MODEL_MANAGED:
                e[k] = v
    e["id"] = row.mid
    e["name"] = row.name or row.mid
    if row.api_override:
        e["api"] = row.api_override
        if provider_base and provider_api and row.api_override != provider_api:
            e["baseUrl"] = base_for_api(row.api_override, provider_base)
    e["reasoning"] = bool(row.reasoning)
    tlm = {}
    for lv in LEVELS:
        st = row.tlm.get(lv, "keep")
        if st == "no":
            tlm[lv] = None
        elif st == "yes":
            tlm[lv] = lv
    if row.reasoning and tlm:
        e["thinkingLevelMap"] = tlm
    e["input"] = ["text", "image"] if row.input_image else ["text"]
    if not row.context_window or row.context_window <= 0:
        raise ValueError("模型 %s 的上下文长度必须大于 0" % row.mid)
    if not row.max_tokens or row.max_tokens <= 0:
        raise ValueError("模型 %s 的最大输出必须大于 0" % row.mid)
    e["contextWindow"] = int(row.context_window)
    e["maxTokens"] = int(row.max_tokens)
    src = row.matched.model if row.matched else None
    if src:
        cost = src.get("cost")
        if isinstance(cost, dict) and cost:
            e["cost"] = cost
        compat = {k: v for k, v in (src.get("compat") or {}).items()
                  if k not in COMPAT_STRIP}
        if compat:
            e["compat"] = compat
    # 少数协议的模型自带一份对应协议的中转预设（服务商级预设只能照顾主协议）
    if relay_options and row.api_override and provider_api \
            and row.api_override != provider_api:
        preset = relay_compat(row.api_override, relay_options)
        if preset:
            merged = dict(e.get("compat") or {})
            merged.update(preset)
            e["compat"] = merged
    return e


def build_provider(base_url, api, api_key, model_dicts,
                   headers_text="", auth_header=False, existing=None,
                   relay_options=None):
    """生成服务商条目。

    existing 传入原有条目时，name、compat、modelOverrides、oauth 等
    工具不管理的字段会原样保留，不会因为刷新写入而丢掉。
    """
    p = {}
    if isinstance(existing, dict):
        for k, v in existing.items():
            if k not in PROVIDER_MANAGED:
                p[k] = v
    p["baseUrl"] = base_url.strip().rstrip("/")
    p["api"] = api
    key = (api_key or "").strip()
    if key:
        p["apiKey"] = key
    if auth_header:
        p["authHeader"] = True
    hs = parse_headers(headers_text)
    if hs:
        p["headers"] = hs
    if relay_options is not None:
        compat = dict(p.get("compat") or {})
        table = RELAY_COMPAT_ANTHROPIC if api == "anthropic-messages" \
            else RELAY_COMPAT_OPENAI
        for name, (field, value) in table.items():
            if relay_options.get(name):
                compat[field] = value
            elif compat.get(field) == value:
                compat.pop(field)      # 取消勾选就把预设写过的值清掉
        if compat:
            p["compat"] = compat
        else:
            p.pop("compat", None)
    p["models"] = model_dicts
    return p


def parse_headers(text):
    """解析自定义请求头。空值和 ... 这类没填完的示例会被丢掉。"""
    if isinstance(text, dict):
        data = text
    else:
        text = (text or "").strip()
        if not text:
            return {}
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("请求头必须是 JSON 对象")
    out = {}
    for k, v in data.items():
        k = str(k).strip()
        v = "" if v is None else str(v).strip()
        if not k or v in HEADER_PLACEHOLDERS:
            continue
        out[k] = v
    return out


def headers_to_text(headers):
    """把已有请求头回填成界面文本框里的内容。"""
    if not headers:
        return ""
    return json.dumps(headers, ensure_ascii=False, indent=2)


# 说明里写的是字母、数字、下划线、点、短横线，所以用 ASCII 模式，
# 否则 \w 会把中文也放进来，和提示文字不一致
PROVIDER_NAME_RE = re.compile(r"^[\w.-]{1,64}$", re.ASCII)


def validate_provider_name(name):
    if not name or not PROVIDER_NAME_RE.match(name):
        raise ValueError(
            "服务商标识只能用字母、数字、下划线、点、短横线，1 到 64 个字符")


def _backup_pattern(path):
    return "%s.backup-*.json" % os.path.splitext(path)[0]


def list_backups(path):
    """按时间从新到旧列出备份文件。"""
    files = glob.glob(_backup_pattern(path))
    return sorted(files, key=lambda f: os.path.getmtime(f), reverse=True)


def prune_backups(path, keep=BACKUP_KEEP):
    """只留最近 keep 份备份，返回删掉的文件列表。"""
    removed = []
    for f in list_backups(path)[keep:]:
        try:
            os.remove(f)
            removed.append(f)
        except OSError:
            pass
    return removed


def backup(path, keep=BACKUP_KEEP):
    if not os.path.exists(path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = "%s.backup-%s.json" % (os.path.splitext(path)[0], stamp)
    n = 2
    while os.path.exists(dst):
        dst = "%s.backup-%s-%d.json" % (os.path.splitext(path)[0], stamp, n)
        n += 1
    shutil.copy2(path, dst)
    prune_backups(path, keep)
    return dst


def load_config(path):
    if not os.path.exists(path):
        return {"providers": {}}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("models.json 内容格式不对（顶层应为对象）")
    data.setdefault("providers", {})
    return data


def check_before_write(data, baseline=None):
    """写盘前自检，返回提醒列表；发现致命问题直接抛异常。

    baseline 传入现有配置时只拦“新增的”问题：
    如果你的 models.json 本来就有毛病，不能因此连删东西、修东西都做不了，
    只要这次改动没把情况弄得更糟就放行，同时把旧问题当成提醒告诉使用者。
    """
    errors, warnings = validator.check_config(data)
    old = []
    if baseline is not None:
        old, _w = validator.check_config(baseline)
    new = [e for e in errors if e not in old]
    if new:
        raise ValueError(
            "配置自检没通过，为避免 pi 读不了整份配置，这次没有写入：\n\n"
            + "\n".join("· " + e for e in new))
    stale = [e for e in errors if e in old]
    if stale:
        warnings.append("提醒：你现有的 models.json 里本来就有 %d 处 pi 不认的地方，"
                        "这次改动没有加重，但建议尽快修：%s"
                        % (len(stale), stale[0]))
    return warnings


def write_config(path, data, validate=True, keep_backups=BACKUP_KEEP,
                 baseline=None):
    """整体写入配置文件，返回备份路径（没有旧文件时为 None）。"""
    if validate:
        if baseline is None and os.path.exists(path):
            try:
                baseline = load_config(path)
            except Exception:  # noqa: BLE001
                baseline = None
        check_before_write(data, baseline=baseline)
    bak = backup(path, keep=keep_backups)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return bak


def restore_backup(path, backup_path):
    """用某份备份覆盖当前配置。覆盖前会先把当前内容再备份一次。"""
    if not os.path.isfile(backup_path):
        raise ValueError("备份文件不存在：%s" % backup_path)
    with open(backup_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "providers" not in data:
        raise ValueError("这份备份的内容不像 models.json，没有 providers")
    return write_config(path, data)


def merge_write(path, provider_name, provider_obj):
    """合并写入单个服务商，返回备份文件路径。"""
    validate_provider_name(provider_name)
    data = load_config(path)
    data["providers"][provider_name] = provider_obj
    return write_config(path, data)


def to_preview_json(provider_name, provider_obj):
    return json.dumps({provider_name: provider_obj},
                      ensure_ascii=False, indent=2)


def entry_to_row(mid, entry, score):
    row = ModelRow(mid)
    if entry is not None:
        row.apply_template(entry, score)
    else:
        row.apply_manual()
    return row


# ---------- 实测结论回写 ----------

def apply_fixes(model, fixes, provider_api=None, provider_base=None):
    """把实测结论应用到一个模型字典上，返回改动说明列表。"""
    done = []
    for fx in fixes or []:
        kind = fx.get("kind")
        val = fx.get("value")
        if kind == "input_image":
            cur = "image" in (model.get("input") or [])
            if cur == bool(val):
                continue
            model["input"] = ["text", "image"] if val else ["text"]
            done.append("%s：图片输入改为%s" % (model.get("id"),
                                              "支持" if val else "不支持"))
        elif kind == "reasoning":
            if bool(model.get("reasoning")) == bool(val):
                continue
            model["reasoning"] = bool(val)
            if not val:
                model.pop("thinkingLevelMap", None)
            done.append("%s：思考支持改为%s" % (model.get("id"),
                                              "支持" if val else "不支持"))
        elif kind == "maxTokensField":
            compat = dict(model.get("compat") or {})
            if compat.get("maxTokensField") == val:
                continue
            compat["maxTokensField"] = val
            model["compat"] = compat
            done.append("%s：compat.maxTokensField 设为 %s" % (model.get("id"), val))
        elif kind == "contextWindow":
            try:
                v = int(val)
            except (TypeError, ValueError):
                continue
            if v <= 0 or model.get("contextWindow") == v:
                continue
            model["contextWindow"] = v
            done.append("%s：上下文长度改为 %s" % (model.get("id"), "{:,}".format(v)))
        elif kind == "api":
            if not val or model.get("api") == val:
                continue
            model["api"] = val
            # 接口格式变了，地址得跟着变，否则 pi 会拼出错路径
            if provider_base:
                if provider_api and val == provider_api:
                    model.pop("baseUrl", None)
                else:
                    model["baseUrl"] = base_for_api(val, provider_base)
            zh = {"openai-completions": "OpenAI 兼容",
                  "anthropic-messages": "Claude 格式",
                  "google-generative-ai": "Gemini 格式"}.get(val, val)
            done.append("%s：接口格式改为%s%s"
                        % (model.get("id"), zh,
                           "，地址一并修正为 " + model["baseUrl"]
                           if model.get("baseUrl") else ""))
    return done
