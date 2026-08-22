# -*- coding: utf-8 -*-
"""主窗口。"""
import copy
import json
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import catalog as catalog_mod
import fetcher
import matcher
import store
import validator
import version

from .common import (CONFIG_PATH, API_LABEL_LIST, API_ZH, CHECKED, UNCHECKED,
                     FILTER_ALL, FILTER_CHECKED, FILTER_MATCHED,
                     FILTER_UNMATCHED, FILTER_CONFIGURED, FILTER_LIST,
                     HEADER_HINT, fmt_num, scrolled_tree)
from .dialogs import (BackupDialog, EditDialog, ManualPasteDialog,
                      PreviewDialog, ProviderPickerDialog, WriteResultDialog)
from .manage import ManagementDialog


class App:
    def __init__(self, root):
        self.root = root
        root.title(version.title())
        root.geometry("1180x740")
        root.minsize(980, 620)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))

        self.catalog = catalog_mod.Catalog()
        self.rowdata = {}          # iid -> ModelRow
        self.order = []            # iid 的显示顺序
        self._iid_seq = 0
        self.q = queue.Queue()
        self._busy = False
        self.loaded_provider = None      # 当前表格数据来自哪个已有服务商
        self._sort_col = None
        self._sort_desc = False

        self._build_top()
        self._build_advanced()
        self._build_filter()
        self._build_table()
        self._build_bottom()

        self.set_status("官方参数库已加载，共 %d 个型号（%s）"
                        % (len(self.catalog), self.catalog.source_label()))
        root.after(120, self._poll_queue)

    # ---------- 界面搭建 ----------

    def _build_top(self):
        f = ttk.LabelFrame(self.root, text="第 1 步：填写服务商信息")
        f.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(f, text="服务商标识：").grid(row=0, column=0, sticky="e", padx=4, pady=6)
        self.var_name = tk.StringVar()
        e = ttk.Entry(f, width=14, textvariable=self.var_name)
        e.grid(row=0, column=1, sticky="w")
        self.var_name.trace_add("write", self._name_changed)
        self._tip(e, "配置文件里的名字，用字母或数字，例如 mysvc、relay2")

        ttk.Label(f, text="接口地址：").grid(row=0, column=2, sticky="e", padx=4)
        self.var_url = tk.StringVar()
        e = ttk.Entry(f, width=42, textvariable=self.var_url)
        e.grid(row=0, column=3, sticky="we", padx=(0, 4))
        self._tip(e, "服务商给你的地址，例如 https://xx.com/v1")

        ttk.Label(f, text="密钥：").grid(row=0, column=4, sticky="e", padx=4)
        keyf = ttk.Frame(f)
        keyf.grid(row=0, column=5, sticky="we")
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(keyf, width=26, textvariable=self.var_key, show="•")
        self.ent_key.pack(side="left", fill="x", expand=True)
        self.var_showkey = tk.BooleanVar(value=False)
        cbk = ttk.Checkbutton(keyf, text="显示", variable=self.var_showkey,
                              command=self._toggle_key)
        cbk.pack(side="left", padx=(4, 0))
        self._tip(self.ent_key, "密钥默认打码显示，录屏或截图前不用担心；勾“显示”可核对")

        ttk.Label(f, text="接口格式：").grid(row=1, column=0, sticky="e", padx=4, pady=6)
        self.var_api = tk.StringVar(value=API_LABEL_LIST[0])
        cb = ttk.Combobox(f, width=14, state="readonly",
                          textvariable=self.var_api, values=API_LABEL_LIST)
        cb.grid(row=1, column=1, sticky="w")
        self._tip(cb, "不确定就选自动检测，工具会三种都试一遍")

        self.var_claude_auto = tk.BooleanVar(value=True)
        cb2 = ttk.Checkbutton(
            f, text="Claude 系模型自动改走 Claude 协议",
            variable=self.var_claude_auto)
        cb2.grid(row=2, column=1, sticky="w")
        self._tip(cb2, "很多中转的 Claude 模型只支持 Anthropic 协议；"
                       "若你的中转两种协议都支持，关掉此项可全部跟随服务商")

        self.var_split = tk.BooleanVar(value=False)
        cb3 = ttk.Checkbutton(
            f, text="协议不同的模型拆成独立服务商",
            variable=self.var_split)
        cb3.grid(row=2, column=2, columnspan=2, sticky="w")
        self._tip(cb3, "默认不拆：pi 允许模型自带接口格式和地址，"
                       "一个服务商里就能混用两种协议，/model 列表更干净；"
                       "只有当两种协议需要不同伪装头时才需要拆")

        b = ttk.Button(f, text="清空重置", command=self.on_clear)
        b.grid(row=2, column=4, columnspan=2, sticky="w")
        self._tip(b, "清空服务商标识、地址、密钥和整个模型表格，换一家重新来")

        self.btn_fetch = ttk.Button(f, text="第 2 步：拉取模型列表", command=self.on_fetch)
        self.btn_fetch.grid(row=1, column=2, columnspan=2, sticky="w", padx=4)

        self.btn_adv = ttk.Button(f, text="高级选项", command=self._toggle_advanced)
        self.btn_adv.grid(row=1, column=4, sticky="w", padx=4)

        ttk.Button(f, text="导入已配置…",
                   command=self.on_import_existing).grid(row=1, column=5,
                                                        sticky="w", padx=4)
        f.columnconfigure(3, weight=1)
        f.columnconfigure(5, weight=1)

    def _toggle_key(self):
        self.ent_key.config(show="" if self.var_showkey.get() else "•")

    def _build_advanced(self):
        self.frm_adv = ttk.LabelFrame(self.root, text="高级选项（一般不用动）")
        self.var_auth_header = tk.BooleanVar(value=False)
        self.frm_adv.pack_forget()
        inner = ttk.Frame(self.frm_adv)
        inner.pack(fill="both", expand=True, padx=8, pady=6)

        # 中转兼容预设：pi-ai 对不在白名单里的中转会按标准 OpenAI 对待，
        # 发 developer 角色、发 max_completion_tokens，而大多数中转两样都不认。
        self.relay_vars = {}
        fp = ttk.LabelFrame(inner, text="第三方中转兼容预设（写在服务商级 compat）")
        fp.pack(fill="x", pady=(0, 6))
        order = ["no_developer_role", "max_tokens_field",
                 "tolerate_no_finish_reason", "no_eager_tool_streaming",
                 "allow_empty_signature"]
        for i, key in enumerate(order):
            v = tk.BooleanVar(value=key in store.RELAY_COMPAT_DEFAULT_ON)
            self.relay_vars[key] = v
            label = store.RELAY_COMPAT_LABEL[key]
            if key in store.RELAY_COMPAT_ANTHROPIC:
                label += "（仅 Claude 格式）"
            cb = ttk.Checkbutton(fp, text=label, variable=v)
            cb.grid(row=i // 2, column=i % 2, sticky="w", padx=6, pady=1)
        ttk.Label(fp, foreground="#888", wraplength=900, justify="left",
                  text="前两项默认勾上：pi 把未知中转当成标准 OpenAI，会发 developer 角色和 "
                       "max_completion_tokens，多数中转不认。导入已有服务商时这里会按原配置回填。"
                  ).grid(row=3, column=0, columnspan=2, sticky="w", padx=6)

        ttk.Label(inner, text="自定义请求头（JSON 格式，选填）：").pack(anchor="w")
        ttk.Label(inner, foreground="#888", wraplength=900, justify="left",
                  text=HEADER_HINT).pack(anchor="w")
        # 默认留空。以前这里预填了示例，示例本身是合法 JSON，
        # 结果每个写入的服务商都被塞了一个假的 User-Agent。
        self.txt_headers = tk.Text(inner, height=4)
        self.txt_headers.pack(fill="x")
        ttk.Checkbutton(inner, text="自动添加 Bearer 认证头（authHeader，Claude 格式中转偶尔需要）",
                        variable=self.var_auth_header).pack(anchor="w", pady=4)

    def _relay_options(self):
        return {k: v.get() for k, v in self.relay_vars.items()}

    def _toggle_advanced(self):
        if self.frm_adv.winfo_ismapped():
            self.frm_adv.pack_forget()
        else:
            self.frm_adv.pack(fill="x", padx=10, pady=4, before=self.frm_filter)

    def _build_filter(self):
        self.frm_filter = ttk.Frame(self.root)
        self.frm_filter.pack(fill="x", padx=10, pady=(4, 0))
        ttk.Label(self.frm_filter, text="搜索：").pack(side="left")
        self.var_search = tk.StringVar()
        e = ttk.Entry(self.frm_filter, width=26, textvariable=self.var_search)
        e.pack(side="left")
        self.var_search.trace_add("write", lambda *_: self.apply_filter())
        self._tip(e, "按模型 ID 或显示名筛选，中转返回几百个模型时很有用")
        ttk.Button(self.frm_filter, text="清除",
                   command=lambda: self.var_search.set("")).pack(side="left", padx=4)
        self.var_filter = tk.StringVar(value=FILTER_ALL)
        cb = ttk.Combobox(self.frm_filter, width=12, state="readonly",
                          textvariable=self.var_filter, values=FILTER_LIST)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda _: self.apply_filter())
        self.var_shown = tk.StringVar(value="")
        ttk.Label(self.frm_filter, textvariable=self.var_shown,
                  foreground="#888").pack(side="left", padx=8)
        ttk.Label(self.frm_filter, foreground="#888",
                  text="点列标题可排序").pack(side="right")

    def _build_table(self):
        self.frm_table = ttk.LabelFrame(
            self.root, text="第 3 步：选择模型（点行首方格勾选，双击行可修改参数）")
        self.frm_table.pack(fill="both", expand=True, padx=10, pady=4)

        cols = ("sel", "id", "match", "ctx", "maxt", "think", "img")
        heads = {"sel": "选", "id": "模型 ID", "match": "识别结果",
                 "ctx": "上下文长度", "maxt": "最大输出",
                 "think": "思考档位", "img": "看图"}
        widths = {"sel": 36, "id": 250, "match": 230, "ctx": 92,
                  "maxt": 92, "think": 230, "img": 52}
        frm, self.tree = scrolled_tree(
            self.frm_table, cols, heads, widths,
            stretch=("id", "match", "think"),
            center=("sel", "img", "ctx", "maxt"), height=13,
            on_heading=self.sort_by)
        frm.pack(side="left", fill="both", expand=True)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<space>", self._on_space)

        bar = ttk.Frame(self.frm_table)
        bar.pack(side="left", fill="y", padx=6)
        ttk.Button(bar, text="全选", command=lambda: self.check_all(True)).pack(fill="x", pady=2)
        ttk.Button(bar, text="全不选", command=lambda: self.check_all(False)).pack(fill="x", pady=2)
        ttk.Button(bar, text="移除选中行", command=self._remove_rows).pack(fill="x", pady=2)
        ttk.Button(bar, text="手动粘贴 ID", command=self.on_paste).pack(fill="x", pady=(12, 2))
        self.var_count = tk.StringVar(value="已选 0 个")
        ttk.Label(bar, textvariable=self.var_count).pack(pady=6)
        ttk.Label(bar, foreground="#888", text="勾选和排序\n只影响写入范围",
                  justify="center").pack(pady=2)

    def _build_bottom(self):
        f = ttk.Frame(self.root)
        f.pack(fill="x", padx=10, pady=(4, 2))
        self.btn_write = ttk.Button(f, text="第 4 步：写入 pi 配置（先预览，自动备份）",
                                   command=self.on_write)
        self.btn_write.pack(side="left")
        ttk.Button(f, text="管理 / 删除已有配置",
                   command=self.on_manage).pack(side="left", padx=10)
        ttk.Button(f, text="备份管理", command=self.on_backups).pack(side="left")
        ttk.Label(f, foreground="#888",
                  text="配置文件：%s" % CONFIG_PATH).pack(side="left", padx=12)
        self.lbl_status = ttk.Label(self.root, relief="sunken", anchor="w",
                                    padding=(6, 4))
        self.lbl_status.pack(fill="x", padx=10, pady=(2, 10))

    def on_manage(self):
        ManagementDialog(self)

    def on_backups(self):
        BackupDialog(self.root)

    def _tip(self, widget, text):
        widget.bind("<Enter>", lambda _: self.set_status(text))

    # ---------- 自动规则 ----------

    @staticmethod
    def _is_claude(row):
        parts = [row.mid, row.name or ""]
        if row.matched is not None:
            m = row.matched.model
            parts.append(m.get("id", ""))
            parts.append(m.get("name", ""))
        return "claude" in " ".join(parts).lower()

    def _apply_auto_rules(self):
        """Claude 系模型自动改走 Claude 协议。

        仅在拉取列表和手动粘贴后调用；用户手动改过的覆盖值不碰；
        服务商本身就是 Claude 格式时无意义，直接跳过。
        """
        if not self.var_claude_auto.get():
            return
        if fetcher.API_LABELS.get(self.var_api.get()) == fetcher.API_ANTHROPIC:
            return
        for iid, r in self.rowdata.items():
            if r.api_override is None and self._is_claude(r):
                r.api_override = fetcher.API_ANTHROPIC
                self.refresh_row(iid)

    # ---------- 导入已有服务商 / 清空 ----------

    def _name_changed(self, *_):
        if self.loaded_provider and self.var_name.get() != self.loaded_provider:
            self.loaded_provider = None

    def on_import_existing(self):
        if self._busy:
            return
        try:
            data = store.load_config(CONFIG_PATH)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("读取失败", str(e))
            return
        if not data["providers"]:
            messagebox.showinfo("提示", "models.json 里还没有任何服务商")
            return
        ProviderPickerDialog(self, data)

    def load_existing_provider(self, data, name):
        """把已配置的服务商装进主窗口：填好信息、列出已配模型，再自动拉最新列表。"""
        p = data["providers"].get(name)
        if not p:
            return
        self._clear_table_only()
        self.var_name.set(name)
        self.var_url.set(p.get("baseUrl", ""))
        self.var_key.set(p.get("apiKey", ""))
        self.var_api.set(fetcher.LABEL_BY_API.get(p.get("api"), API_LABEL_LIST[0]))
        # 请求头和 authHeader 一并带回来，否则重写时会把它们弄丢
        self.txt_headers.delete("1.0", "end")
        self.txt_headers.insert("1.0", store.headers_to_text(p.get("headers")))
        self.var_auth_header.set(bool(p.get("authHeader")))
        # 兼容预设按原配置回填，避免刷新写入时把人家手调过的 compat 改掉
        state = store.relay_compat_state(p.get("api"), p.get("compat"))
        for key, var in self.relay_vars.items():
            if key in state:
                var.set(bool(state[key]))
        # 原来是不是被工具拆过：同名加 -claude 之类的条目还在就保持拆分习惯
        self.var_split.set(any(
            (name + suf) in data["providers"]
            for suf in ("-claude", "-gemini", "-openai")))
        n = 0
        for m in p.get("models") or []:
            row = store.row_from_config(m)
            row.checked = True
            if self._register_row(row):
                n += 1
        self.loaded_provider = name
        self._refresh_count()
        keep = [k for k in p.keys()
                if k not in store.PROVIDER_MANAGED]
        extra = "，原有 %s 字段会保留" % "、".join(keep) if keep else ""
        self.set_status("已载入 %s 的 %d 个已配置模型%s，正在拉取最新列表……"
                        % (name, n, extra))
        self.on_fetch()

    def _clear_table_only(self):
        self.rowdata.clear()
        self.order = []
        self.tree.delete(*self.tree.get_children())
        self._refresh_count()

    def on_clear(self):
        if self._busy:
            return
        if self.rowdata and not messagebox.askyesno(
                "清空", "表格里有 %d 个模型，确定全部清空？" % len(self.rowdata)):
            return
        self.var_name.set("")
        self.var_url.set("")
        self.var_key.set("")
        self.var_api.set(API_LABEL_LIST[0])
        self.loaded_provider = None
        self._clear_table_only()
        self.txt_headers.delete("1.0", "end")
        self.var_auth_header.set(False)
        for key, var in self.relay_vars.items():
            var.set(key in store.RELAY_COMPAT_DEFAULT_ON)
        self.var_split.set(False)
        self.var_search.set("")
        self.var_filter.set(FILTER_ALL)
        self.set_status("已清空，可以配置下一个服务商。")

    # ---------- 状态栏 ----------

    def set_status(self, text):
        self.lbl_status.config(text=text)

    # ---------- 模型行 ----------

    def _register_row(self, row):
        """把准备好的 ModelRow 放进表格；同 ID 已存在时返回 None。"""
        for r in self.rowdata.values():
            if r.mid == row.mid:
                return None
        self._iid_seq += 1
        iid = "r%d" % self._iid_seq
        self.rowdata[iid] = row
        self.order.append(iid)
        self.tree.insert("", "end", iid=iid, values=self._row_values(iid))
        return iid

    def add_row(self, mid, entry=None, score=0.0):
        return self._register_row(store.entry_to_row(mid, entry, score))

    def _row_values(self, iid):
        r = self.rowdata[iid]
        if r.matched is not None:
            match_txt = "%s（%s）" % (r.matched.display(),
                                     matcher.score_label(r.score))
        else:
            match_txt = "未识别，需手动填"
        mid_txt = r.mid
        marks = []
        if r.from_config:
            marks.append("已配置")
        if r.api_override:
            zh = API_ZH.get(r.api_override)
            if zh:
                marks.append(zh)
        if marks:
            mid_txt += "\n〔%s〕" % "·".join(marks)
        # 只把明确表态的档位交给描述函数，"默认"档不能显示成不可用
        think_txt = catalog_mod.describe_thinking(r.explicit_tlm(), r.reasoning)
        return (CHECKED if r.checked else UNCHECKED, mid_txt, match_txt,
                fmt_num(r.context_window), fmt_num(r.max_tokens),
                think_txt, "能" if r.input_image else "不能")

    def refresh_row(self, iid):
        if self.tree.exists(iid):
            self.tree.item(iid, values=self._row_values(iid))

    def _refresh_count(self):
        n = sum(1 for r in self.rowdata.values() if r.checked)
        self.var_count.set("已选 %d 个" % n)
        self.var_shown.set("共 %d 个模型" % len(self.rowdata))

    # ---------- 搜索、筛选、排序 ----------

    def apply_filter(self):
        q = self.var_search.get().strip().lower()
        mode = self.var_filter.get()
        shown = 0
        for iid in self.order:
            r = self.rowdata.get(iid)
            if r is None:
                continue
            ok = True
            if q and q not in r.mid.lower() and q not in (r.name or "").lower():
                ok = False
            if ok and mode == FILTER_CHECKED:
                ok = r.checked
            elif ok and mode == FILTER_MATCHED:
                ok = r.matched is not None
            elif ok and mode == FILTER_UNMATCHED:
                ok = r.matched is None
            elif ok and mode == FILTER_CONFIGURED:
                ok = r.from_config
            if ok:
                self.tree.reattach(iid, "", "end")
                shown += 1
            else:
                self.tree.detach(iid)
        total = len(self.rowdata)
        self.var_shown.set("共 %d 个模型%s"
                           % (total, "，当前显示 %d 个" % shown if shown != total else ""))

    def sort_by(self, col):
        if col == "sel":
            return
        self._sort_desc = not self._sort_desc if self._sort_col == col else False
        self._sort_col = col

        def key(iid):
            r = self.rowdata[iid]
            if col == "id":
                return r.mid.lower()
            if col == "match":
                return (-r.score, r.mid.lower())
            if col == "ctx":
                return r.context_window
            if col == "maxt":
                return r.max_tokens
            if col == "think":
                return (0 if r.reasoning else 1, r.mid.lower())
            if col == "img":
                return (0 if r.input_image else 1, r.mid.lower())
            return r.mid.lower()

        self.order.sort(key=key, reverse=self._sort_desc)
        self.apply_filter()

    def _on_click(self, ev):
        if self.tree.identify("region", ev.x, ev.y) != "cell":
            return
        col = self.tree.identify_column(ev.x)
        iid = self.tree.identify_row(ev.y)
        if not iid or col != "#1":
            return
        row = self.rowdata[iid]
        row.checked = not row.checked
        self.refresh_row(iid)
        self._refresh_count()
        if self.var_filter.get() == FILTER_CHECKED:
            self.apply_filter()

    def _on_double(self, ev):
        iid = self.tree.identify_row(ev.y)
        if not iid:
            return
        EditDialog(self.root, self.catalog, self.rowdata[iid],
                   prefer_api=fetcher.API_LABELS.get(self.var_api.get()),
                   on_ok=lambda _r, i=iid: self.refresh_row(i))

    def _on_space(self, _):
        iid = self.tree.focus()
        if iid and iid in self.rowdata:
            self.rowdata[iid].checked = not self.rowdata[iid].checked
            self.refresh_row(iid)
            self._refresh_count()

    def check_all(self, flag):
        """只影响当前显示出来的行，配合搜索可以批量勾一类模型。"""
        for iid in self.tree.get_children():
            self.rowdata[iid].checked = flag
            self.refresh_row(iid)
        self._refresh_count()
        if self.var_filter.get() == FILTER_CHECKED:
            self.apply_filter()

    def _remove_rows(self):
        for iid in self.tree.selection():
            self.rowdata.pop(iid, None)
            if iid in self.order:
                self.order.remove(iid)
            self.tree.delete(iid)
        self._refresh_count()

    # ---------- 拉取 ----------

    def on_fetch(self):
        if self._busy:
            return
        url = self.var_url.get().strip()
        key = self.var_key.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先填接口地址")
            return
        api_type = fetcher.API_LABELS.get(self.var_api.get(), fetcher.API_AUTO)
        self._busy = True
        self.btn_fetch.config(state="disabled")
        self.set_status("正在连接服务商并拉取模型列表……")
        threading.Thread(target=self._fetch_worker, args=(url, key, api_type),
                         daemon=True).start()

    def _fetch_worker(self, url, key, api_type):
        try:
            kind, ids, eff_base = fetcher.fetch_models(url, key, api_type)
            self.q.put(("fetched", kind, ids, eff_base))
        except Exception as e:  # noqa: BLE001
            self.q.put(("fetch_error", fetcher.redact(e, key)))

    def _match_worker(self, ids, prefer):
        """识别在后台线程跑。prefer 必须由主线程取好再传进来，
        子线程里读 tkinter 变量不安全。"""
        out = []
        for i, mid in enumerate(ids, 1):
            cands = matcher.match(self.catalog, mid, prefer_api=prefer, top=1)
            best = cands[0] if cands else None
            out.append((mid, best.entry if best else None,
                        best.score if best else 0.0))
            if i % 20 == 0:
                self.q.put(("progress", "正在识别官方参数 %d/%d……" % (i, len(ids))))
        self.q.put(("matched", out))

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "fetched":
                    _, api_kind, ids, eff_base = msg
                    self.var_api.set(fetcher.LABEL_BY_API[api_kind])
                    if api_kind == fetcher.API_ANTHROPIC \
                            and eff_base.rstrip("/").endswith("/v1"):
                        # pi 的 Claude 格式要求地址不带 /v1，它会自己拼接
                        eff_base = eff_base.rstrip("/")[:-3]
                    if eff_base and eff_base.rstrip("/") != self.var_url.get().strip().rstrip("/"):
                        self.var_url.set(eff_base)
                        self.set_status("已自动把地址修正为 %s" % eff_base)
                    else:
                        self.set_status("拉取成功，共 %d 个模型，正在识别官方参数……"
                                        % len(ids))
                    prefer = fetcher.API_LABELS.get(self.var_api.get())
                    threading.Thread(target=self._match_worker,
                                     args=(ids, prefer), daemon=True).start()
                elif kind == "progress":
                    self.set_status(msg[1])
                elif kind == "matched":
                    rows = msg[1]
                    added = 0
                    for mid, entry, score in rows:
                        if self.add_row(mid, entry, score):
                            added += 1
                    self._apply_auto_rules()
                    self._refresh_count()
                    self.apply_filter()
                    self._busy = False
                    self.btn_fetch.config(state="normal")
                    n = sum(1 for r in self.rowdata.values()
                            if r.api_override == fetcher.API_ANTHROPIC)
                    extra = "（其中 %d 个 Claude 系模型已自动改走 Claude 协议）" % n \
                        if n else ""
                    self.set_status("完成：拉到 %d 个模型，新增 %d 行%s。"
                                    "点行首方格勾选要用的，双击行可修改参数。"
                                    % (len(rows), added, extra))
                elif kind == "fetch_error":
                    self._busy = False
                    self.btn_fetch.config(state="normal")
                    self.set_status("拉取失败：" + msg[1])
                    messagebox.showerror("拉取失败", msg[1])
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    # ---------- 手动粘贴 ----------

    def on_paste(self):
        ManualPasteDialog(self)

    def add_manual_ids(self, ids):
        prefer = fetcher.API_LABELS.get(self.var_api.get())
        added = 0
        for mid in ids:
            cands = matcher.match(self.catalog, mid, prefer_api=prefer, top=1)
            best = cands[0] if cands else None
            if self.add_row(mid, best.entry if best else None,
                            best.score if best else 0.0):
                added += 1
        self._apply_auto_rules()
        self._refresh_count()
        self.apply_filter()
        self.set_status("手动添加了 %d 个模型。" % added)

    # ---------- 写入 ----------

    def on_write(self):
        if self._busy:
            return
        name = self.var_name.get().strip()
        url = self.var_url.get().strip()
        key = self.var_key.get().strip()
        chosen = [r for r in self.rowdata.values() if r.checked]
        try:
            store.validate_provider_name(name)
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        if not url:
            messagebox.showwarning("提示", "接口地址不能为空")
            return
        if not key:
            messagebox.showwarning(
                "密钥不能为空",
                "pi 要求密钥至少 1 个字符。如果写成空的，pi 会判定整份 "
                "models.json 非法，连其他服务商也一起用不了。\n\n"
                "请填上密钥再写入。")
            return
        if not chosen:
            messagebox.showwarning("提示", "请先在表格里勾选至少一个模型")
            return
        try:
            for r in chosen:
                store.build_model_dict(r)  # 提前校验数值，避免写一半报错
        except ValueError as e:
            messagebox.showwarning("参数有误", str(e))
            return
        api = fetcher.API_LABELS.get(self.var_api.get(), fetcher.API_AUTO)
        if api == fetcher.API_AUTO:
            messagebox.showwarning("提示", "请先拉取一次列表，让工具确定接口格式")
            return
        try:
            cur_cfg = store.load_config(CONFIG_PATH)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("读取失败", "读取现有配置失败：%s" % e)
            return
        target = self._resolve_target(name, url, key, cur_cfg["providers"])
        if not target:
            return
        name = target
        try:
            entries = self._build_provider_entries(
                name, url, api, key, chosen,
                self.txt_headers.get("1.0", "end"),
                self.var_auth_header.get(), cur_cfg["providers"])
        except (ValueError, json.JSONDecodeError) as e:
            messagebox.showwarning("高级选项有误", "自定义请求头格式不对：%s" % e)
            return
        notes = self._build_notes(entries, cur_cfg)

        # 写盘前先自检：pi 只要发现一处非法就会丢掉整份配置。
        # 只拦这次改动引入的新问题，配置里原本就有的毛病不能连带把删改都堵死。
        candidate = copy.deepcopy(cur_cfg)
        for gname, prov in entries:
            candidate["providers"][gname] = prov
        try:
            warnings = store.check_before_write(candidate, baseline=cur_cfg)
        except ValueError as e:
            messagebox.showerror("自检没通过，已阻止写入", str(e))
            self.set_status("自检未通过，已阻止写入。")
            return
        pi_note = None
        if validator.pi_available():
            self.set_status("正在用 pi 自带的校验器复核这份配置……")
            self.root.update_idletasks()
            ok, msg = validator.pi_validate_data(candidate)
            if ok is False:
                base_ok, _bmsg = validator.pi_validate_data(cur_cfg)
                if base_ok is not False:
                    messagebox.showerror(
                        "pi 校验没通过，已阻止写入",
                        "pi 认为这份配置不合法，写进去会让所有服务商都失效：\n\n%s" % msg)
                    self.set_status("pi 校验未通过，已阻止写入。")
                    return
                warnings = list(warnings) + [
                    "你现有的 models.json pi 本来就读不了（%s），本次写入不会加重，"
                    "但建议尽快修" % msg.splitlines()[0][:120]]
            elif ok:
                pi_note = "已用 pi 自带校验器复核通过。"
            # schema 之外还有一层：pi 组装模型时缺 api、缺 baseUrl 会把该服务商整包丢掉。
            # 在临时沙盒里让真正的 pi 先读一遍，把它的警告拿出来看。
            dry_ok, dry_txt = validator.pi_dry_run(candidate)
            if dry_ok is False:
                bad = [l for l in dry_txt.splitlines()
                       if "warning" in l.lower() or "error" in l.lower()
                       or l.startswith("Provider")]
                warnings = list(warnings) + [
                    "pi 试读这份配置时有话说：" + " / ".join(bad[:3])]
            elif dry_ok:
                pi_note = (pi_note or "") + "沙盒里让 pi 真读一遍也没有警告。"
            self.set_status("复核完成，请在预览里确认。")
        PreviewDialog(self, entries, notes, warnings, pi_note)

    def _build_notes(self, entries, cur_cfg):
        """与已有条目对账：未在表格出现的原有模型自动保留，并生成变更说明。"""
        notes = []
        for gname, prov in entries:
            ex = cur_cfg["providers"].get(gname)
            if not ex:
                notes.append("新增服务商 %s（共 %d 个模型）"
                             % (gname, len(prov["models"])))
                continue
            old_models = ex.get("models") or []
            old_ids = [m.get("id") for m in old_models]
            have = {m.get("id") for m in prov["models"]}
            if gname != self.loaded_provider:
                missing = [m for m in old_models if m.get("id") not in have]
                if missing:
                    prov["models"] = list(prov["models"]) + missing
                    s = "、".join(m.get("id", "?") for m in missing[:6]) \
                        + ("…" if len(missing) > 6 else "")
                    notes.append("%s：自动保留原有 %d 个模型（%s）"
                                 % (gname, len(missing), s))
            new_ids = [m.get("id") for m in prov["models"]]
            rem = [i for i in old_ids if i not in new_ids]
            add = [i for i in new_ids if i not in old_ids]
            if rem:
                s = "、".join(rem[:8]) + ("…" if len(rem) > 8 else "")
                notes.append("%s 将移除 %d 个模型：%s" % (gname, len(rem), s))
            if add:
                s = "、".join(add[:8]) + ("…" if len(add) > 8 else "")
                notes.append("%s 将新增 %d 个模型：%s" % (gname, len(add), s))
            if not rem and not add:
                notes.append("%s：内容无变化" % gname)
            keep = [k for k in ex.keys() if k not in store.PROVIDER_MANAGED]
            if keep:
                notes.append("%s：原有 %s 字段原样保留" % (gname, "、".join(keep)))
        for gname, prov in entries:
            mixed = [m for m in prov["models"] if m.get("baseUrl")]
            if mixed:
                notes.append("%s：其中 %d 个模型自带接口格式和地址（%s），"
                             "不用另建服务商"
                             % (gname, len(mixed),
                                "、".join(m.get("id", "?") for m in mixed[:5])
                                + ("…" if len(mixed) > 5 else "")))
            compat = prov.get("compat")
            if compat:
                notes.append("%s：服务商级兼容项写入 %s"
                             % (gname, "、".join("%s=%s" % (k, v)
                                                for k, v in compat.items())))
        return notes

    def _resolve_target(self, name, url, key, provs):
        """决定这次写入落到哪个服务商条目上，返回最终名字；取消时返回 None。

        规则：
        1. 同名同密钥 → 直接用它（表格勾选即最终内容）；
        2. 同名但密钥不同 → 不能覆盖，建议改用新名字；
        3. 名字没用过，但地址和密钥与某现有条目完全一致且接口格式相同 → 并入它；
        4. 地址相同但密钥不同 → 确认后按新服务商独立写入。
        """
        ex = provs.get(name)
        if ex is not None:
            if (ex.get("apiKey") or "") == key:
                return name
            i = 2
            while "%s-%d" % (name, i) in provs:
                i += 1
            newname = "%s-%d" % (name, i)
            if messagebox.askyesno(
                    "密钥不同",
                    "服务商标识 %s 已存在，但密钥不一样，不能覆盖。\n\n"
                    "点“是”自动改用新标识 %s 继续写入；点“否”返回自己修改。"
                    % (name, newname)):
                try:
                    store.validate_provider_name(newname)
                except ValueError as e:
                    messagebox.showwarning("提示", str(e))
                    return None
                self.var_name.set(newname)
                return newname
            return None
        url_n = url.strip().rstrip("/")
        same_full = None
        same_url = False
        for pn, pv in provs.items():
            pu = (pv.get("baseUrl") or "").strip().rstrip("/")
            if pn == name or pu != url_n:
                continue
            if (pv.get("apiKey") or "") == key:
                same_full = pn
                break
            same_url = True
        if same_full:
            if provs[same_full].get("api") == fetcher.API_LABELS.get(
                    self.var_api.get(), ""):
                self.set_status("地址和密钥与已有服务商 %s 一致，本次将并入它。"
                                % same_full)
                return same_full
            return name  # 接口格式不同，不并入，按拆分规则另建
        if same_url:
            if not messagebox.askyesno(
                    "同一地址不同密钥",
                    "地址 %s 已由其他服务商用另一把密钥配置。\n"
                    "点“是”确认按新服务商 %s 独立写入；点“否”返回。" % (url, name)):
                return None
        return name

    def _build_provider_entries(self, name, url, api, key, rows,
                                headers_text, auth_header, provs=None):
        """生成要写入的服务商条目。

        默认不拆分：pi 取地址时模型级 baseUrl 优先于服务商级，所以协议不同的
        模型自带 api 和 baseUrl 就行，不必另建条目，pi 的 /model 列表也更干净。
        勾了“拆成独立服务商”才走旧的分组逻辑（两种协议需要不同伪装头时用）。
        """
        provs = provs or {}
        relay = self._relay_options()
        base0 = url.strip().rstrip("/")
        if not self.var_split.get():
            models = [store.build_model_dict(
                r, provider_api=api, provider_base=base0, relay_options=relay)
                for r in rows]
            prov = store.build_provider(
                store.base_for_api(api, base0), api, key, models,
                headers_text=headers_text, auth_header=auth_header,
                existing=provs.get(name), relay_options=relay)
            return [(name, prov)]

        groups = {}
        order = []
        for r in rows:
            eff = r.api_override or api
            if eff not in groups:
                groups[eff] = []
                order.append(eff)
            groups[eff].append(store.build_model_dict(
                r, provider_api=eff, provider_base=base0, relay_options=relay))
        suffix = {fetcher.API_ANTHROPIC: "-claude",
                  fetcher.API_GOOGLE: "-gemini",
                  fetcher.API_OPENAI: "-openai"}
        entries = []
        for gapi in order:
            gname = name
            if gapi != api:
                gname = (name + suffix.get(gapi, "-x"))[:64]
                store.validate_provider_name(gname)
            entries.append((gname, store.build_provider(
                store.base_for_api(gapi, base0), gapi, key, groups[gapi],
                headers_text=headers_text, auth_header=auth_header,
                existing=provs.get(gname), relay_options=relay)))
        return entries

    def do_write(self, entries):
        """真正落盘，然后让 pi 自己确认一遍。"""
        try:
            data = store.load_config(CONFIG_PATH)
            baseline = copy.deepcopy(data)
            for n, p in entries:
                store.validate_provider_name(n)
                data["providers"][n] = p
            bak = store.write_config(CONFIG_PATH, data, baseline=baseline)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("写入失败", str(e))
            return
        names = [n for n, _ in entries]
        total = sum(len(p["models"]) for _n, p in entries)
        head = "已写入 %s，共 %d 个模型。\n" % ("、".join(names), total)
        head += ("原文件备份在：%s" % bak) if bak else "（原来没有配置文件，这次是新建）"
        ok, detail = validator.pi_list_models(names[0])
        if ok is None:
            WriteResultDialog(self.root, "写入成功", head,
                              "没找到 pi 命令，跳过复核。\n\n%s" % detail,
                              backup_path=bak, on_restore=self._restore, ok=True)
        elif ok:
            WriteResultDialog(self.root, "写入成功并已通过 pi 复核",
                              head + "\n\npi 现在能认到这些模型：",
                              detail, backup_path=bak,
                              on_restore=self._restore, ok=True)
        else:
            WriteResultDialog(self.root, "写入了，但 pi 读不出来",
                              head + "\n\npi 报错了，建议还原到写入前再排查：",
                              detail, backup_path=bak,
                              on_restore=self._restore, ok=False)
        self.set_status("写入成功：%s，共 %d 个模型。在 pi 里按 /model 即可看到新模型。"
                        % ("、".join(names), total))

    def _restore(self, backup_path):
        try:
            bak = store.restore_backup(CONFIG_PATH, backup_path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("还原失败", str(e))
            return
        messagebox.showinfo("已还原", "已还原到写入前的内容。\n\n"
                            "还原前的内容也备份了一份：\n%s" % bak)
        self.set_status("已还原到写入前的配置。")
