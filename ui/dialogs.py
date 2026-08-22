# -*- coding: utf-8 -*-
"""各类对话框：参数编辑、服务商选择、写入预览、手动粘贴、写入结果、备份管理。"""
import json
import os
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

import matcher
import store

from .common import (CONFIG_PATH, api_label, api_from_label, scrolled_tree,
                     read_only_text)


class EditDialog(tk.Toplevel):
    """改一行模型的参数。谁调用都行，改完通过 on_ok 回调把行交回去。"""

    def __init__(self, parent, catalog, row, prefer_api=None, on_ok=None,
                 title_prefix="修改模型参数"):
        super().__init__(parent)
        self.catalog = catalog
        self.row = row
        self.prefer_api = prefer_api
        self.on_ok = on_ok
        self.title("%s：%s" % (title_prefix, row.mid))
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.pending = None  # 搜索后待选的官方条目

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=12, pady=10)

        ttk.Label(frm, text="模型 ID：%s" % row.mid).grid(
            row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(frm, text="显示名：").grid(row=1, column=0, sticky="e", pady=4)
        self.var_name = tk.StringVar(value=row.name)
        ttk.Entry(frm, width=28, textvariable=self.var_name).grid(
            row=1, column=1, columnspan=3, sticky="w")

        ttk.Label(frm, text="上下文长度：").grid(row=2, column=0, sticky="e")
        self.var_ctx = tk.StringVar(value=str(row.context_window))
        ttk.Entry(frm, width=14, textvariable=self.var_ctx).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="（tokens，务必按官方值填）").grid(
            row=2, column=2, columnspan=2, sticky="w")

        ttk.Label(frm, text="最大输出：").grid(row=3, column=0, sticky="e")
        self.var_max = tk.StringVar(value=str(row.max_tokens))
        ttk.Entry(frm, width=14, textvariable=self.var_max).grid(row=3, column=1, sticky="w")

        self.var_reason = tk.BooleanVar(value=row.reasoning)
        ttk.Checkbutton(frm, text="支持扩展思考", variable=self.var_reason,
                        command=self._toggle_reason).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.var_img = tk.BooleanVar(value=row.input_image)
        ttk.Checkbutton(frm, text="能识别图片（多模态）", variable=self.var_img).grid(
            row=4, column=2, columnspan=2, sticky="w", pady=(6, 0))

        self.frm_tlm = ttk.LabelFrame(
            frm, text="思考档位（可用=按档位名发送；不可用=界面里隐藏该档；默认=交给 pi）")
        self.tlm_vars = {}
        zh = store.LEVEL_ZH
        for i, lv in enumerate(store.LEVELS):
            ttk.Label(self.frm_tlm, text="%s（%s）" % (lv, zh[lv])).grid(
                row=i // 4, column=(i % 4) * 2, sticky="e", padx=(8, 2), pady=2)
            v = tk.StringVar(value={"keep": "默认", "yes": "可用",
                                    "no": "不可用"}[row.tlm[lv]])
            self.tlm_vars[lv] = v
            ttk.Combobox(self.frm_tlm, width=6, state="readonly", textvariable=v,
                         values=("默认", "可用", "不可用")).grid(
                row=i // 4, column=(i % 4) * 2 + 1, sticky="w", pady=2)
        self.frm_tlm.grid(row=5, column=0, columnspan=4, sticky="we", pady=6)
        ttk.Label(frm, foreground="#888",
                  text="说明：pi 只认 text 和 image 两类输入，视频识别没有对应配置项。"
                       "思考档位的“默认”表示这一档不写进配置，交给 pi 按厂商惯例处理。"
                       ).grid(row=6, column=0, columnspan=4, sticky="w")

        ttk.Label(frm, text="接口格式：").grid(row=7, column=0, sticky="e", pady=(4, 0))
        self.var_api_ovr = tk.StringVar(value=api_label(row.api_override))
        ttk.Combobox(frm, width=16, state="readonly", textvariable=self.var_api_ovr,
                     values=("跟随服务商", "OpenAI 兼容", "Claude 格式",
                             "Gemini 格式")).grid(row=7, column=1, columnspan=3,
                                                  sticky="w", pady=(4, 0))
        ttk.Label(frm, foreground="#888",
                  text="个别模型与中转其他模型协议不同时（如同一中转里 Claude 模型只支持"
                       " Anthropic 协议），在这里单独指定；写入时会自动拆成独立服务商并修正地址。"
                       ).grid(row=8, column=0, columnspan=4, sticky="w")

        frm2 = ttk.LabelFrame(frm, text="识别结果不对？在这里搜索官方型号重新匹配")
        ttk.Label(frm2, text="关键词：").pack(side="left", padx=4)
        self.var_search = tk.StringVar()
        ent = ttk.Entry(frm2, width=24, textvariable=self.var_search)
        ent.pack(side="left", padx=2)
        ent.bind("<Return>", lambda _: self._search())
        ttk.Button(frm2, text="搜索", command=self._search).pack(side="left", padx=2)
        self.lst = tk.Listbox(frm2, height=5, width=60)
        self.lst.pack(fill="x", padx=4, pady=4)
        self.candidates = []
        ttk.Button(frm2, text="采用选中项（会用官方参数覆盖上面的值）",
                   command=self._adopt).pack(anchor="w", padx=4, pady=(0, 4))
        frm2.grid(row=9, column=0, columnspan=4, sticky="we", pady=4)

        btns = ttk.Frame(frm)
        ttk.Button(btns, text="确定", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left")
        btns.grid(row=10, column=0, columnspan=4, pady=(8, 0))

        self._toggle_reason()
        self.bind("<Escape>", lambda _: self.destroy())

    def _toggle_reason(self):
        state = "normal" if self.var_reason.get() else "disabled"
        for child in self.frm_tlm.winfo_children():
            try:
                child.config(state=state)
            except tk.TclError:
                pass

    def _search(self):
        q = self.var_search.get().strip()
        if not q:
            return
        cands = matcher.match(self.catalog, q, prefer_api=self.prefer_api,
                              top=8, fuzzy_threshold=0.4)
        self.candidates = [c.entry for c in cands]
        self.lst.delete(0, "end")
        for c in cands:
            self.lst.insert("end", "%s   匹配度：%s"
                            % (c.entry.display(), matcher.score_label(c.score)))
        if not cands:
            self.lst.insert("end", "没找到，换个关键词试试")

    def _adopt(self):
        sel = self.lst.curselection()
        if not sel or not self.candidates:
            messagebox.showinfo("提示", "先在列表里点选一个型号", parent=self)
            return
        self.pending = self.candidates[sel[0]]
        self.row.apply_template(self.pending, 1.0)
        self.var_name.set(self.row.name)
        self.var_ctx.set(str(self.row.context_window))
        self.var_max.set(str(self.row.max_tokens))
        self.var_reason.set(self.row.reasoning)
        self.var_img.set(self.row.input_image)
        for lv in store.LEVELS:
            self.tlm_vars[lv].set({"keep": "默认", "yes": "可用",
                                   "no": "不可用"}[self.row.tlm[lv]])
        self._toggle_reason()
        self.title("修改模型参数：%s（已换成 %s）"
                   % (self.row.mid, self.pending.display()))

    def _ok(self):
        try:
            ctx = int(self.var_ctx.get().replace(",", "").strip())
            mx = int(self.var_max.get().replace(",", "").strip())
            if ctx <= 0 or mx <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "上下文长度和最大输出必须是大于 0 的整数",
                                   parent=self)
            return
        r = self.row
        r.name = self.var_name.get().strip() or r.mid
        r.context_window = ctx
        r.max_tokens = mx
        r.reasoning = self.var_reason.get()
        r.input_image = self.var_img.get()
        r.api_override = api_from_label(self.var_api_ovr.get())
        for lv in store.LEVELS:
            r.tlm[lv] = {"默认": "keep", "可用": "yes", "不可用": "no"}[
                self.tlm_vars[lv].get()]
        cb = self.on_ok
        self.destroy()
        if cb:
            cb(r)


class ProviderPickerDialog(tk.Toplevel):
    """从 models.json 里挑一个已有服务商，载入主窗口去刷新或补充模型。"""

    def __init__(self, app, data):
        super().__init__(app.root)
        self.app = app
        self.data = data
        self.title("导入已配置的服务商")
        self.geometry("880x440")
        self.transient(app.root)
        self.grab_set()

        ttk.Label(self, foreground="#555", wraplength=840, justify="left",
                  text="点选一个服务商后点下方按钮：工具会自动填好地址、密钥、请求头，"
                       "把已配置的模型带进表格并勾选，然后自动拉取最新列表。"
                       "新出现的模型会以未勾选状态追加，你勾选后写入即可补充。"
                       "原有的 name、compat、modelOverrides 等字段会原样保留。"
                  ).pack(anchor="w", padx=12, pady=(10, 4))
        cols = ("name", "api", "cnt", "url")
        heads = {"name": "名称", "api": "格式", "cnt": "模型数", "url": "地址"}
        widths = {"name": 130, "api": 150, "cnt": 70, "url": 420}
        frm, self.tv = scrolled_tree(self, cols, heads, widths,
                                     stretch=("url",), center=("cnt",), height=12)
        frm.pack(fill="both", expand=True, padx=12, pady=4)
        self.tv.bind("<Double-1>", lambda _: self._load())

        btns = ttk.Frame(self)
        btns.pack(pady=8)
        ttk.Button(btns, text="载入并检查新模型", command=self._load).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=6)

        for name, p in data["providers"].items():
            self.tv.insert("", "end", iid=name, values=(
                name, p.get("api", "?"), len(p.get("models") or []),
                p.get("baseUrl", "")))
        if data["providers"]:
            first = list(data["providers"].keys())[0]
            self.tv.selection_set(first)
            self.tv.focus(first)

    def _load(self):
        sel = self.tv.selection()
        if not sel:
            messagebox.showinfo("提示", "先点选一个服务商", parent=self)
            return
        name = sel[0]
        self.destroy()
        self.app.load_existing_provider(self.data, name)


class PreviewDialog(tk.Toplevel):
    """写入预览。确认后交给 app.do_write 真正落盘。"""

    def __init__(self, app, entries, notes=None, warnings=None, pi_note=None):
        super().__init__(app.root)
        self.app = app
        self.entries = entries          # [(服务商名, provider对象), ...]
        names = "、".join(n for n, _ in entries)
        self.title("写入预览：%s" % names)
        self.geometry("860x640")
        self.transient(app.root)
        self.grab_set()

        tip = "将把下面这些服务商配置合并写入 models.json（其他服务商保持不变，写入前自动备份）。"
        if len(entries) > 1:
            tip += ("\n注意：勾选的模型里有接口格式不同的，已自动拆成 %d 个服务商条目，"
                    "地址已按各自要求修正。" % len(entries))
        ttk.Label(self, text=tip, wraplength=820, justify="left").pack(
            anchor="w", padx=12, pady=(10, 4))
        if notes:
            nf = ttk.LabelFrame(self, text="本次变更摘要（相对现有配置）")
            nf.pack(fill="x", padx=12)
            ttk.Label(nf, foreground="#b26a00", wraplength=800, justify="left",
                      text="\n".join("· " + n for n in notes)).pack(
                anchor="w", padx=8, pady=6)
        if warnings:
            wf = ttk.LabelFrame(self, text="自检提醒（不影响写入，但建议看一眼）")
            wf.pack(fill="x", padx=12, pady=(6, 0))
            ttk.Label(wf, foreground="#b26a00", wraplength=800, justify="left",
                      text="\n".join("· " + w for w in warnings[:8])).pack(
                anchor="w", padx=8, pady=6)
        if pi_note:
            ttk.Label(self, foreground="#2e7d32", wraplength=820, justify="left",
                      text="· " + pi_note).pack(anchor="w", padx=14, pady=(6, 0))
        txt = tk.Text(self, font=("Consolas", 10))
        txt.pack(fill="both", expand=True, padx=12, pady=4)
        txt.insert("1.0", json.dumps({n: p for n, p in entries},
                                     ensure_ascii=False, indent=2))
        txt.config(state="disabled")
        btns = ttk.Frame(self)
        btns.pack(pady=8)
        ttk.Button(btns, text="确认写入", command=self._write).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=6)

    def _write(self):
        entries = self.entries
        self.destroy()
        self.app.do_write(entries)


class WriteResultDialog(tk.Toplevel):
    """写入完成后的复核结果：pi 实际认到了什么，出问题可以一键还原。"""

    def __init__(self, parent, title, summary, detail, backup_path=None,
                 on_restore=None, ok=True):
        super().__init__(parent)
        self.title(title)
        self.geometry("820x520")
        self.transient(parent)
        self.grab_set()
        ttk.Label(self, wraplength=780, justify="left",
                  foreground=("#2e7d32" if ok else "#c62828"),
                  text=summary, padding=(12, 10)).pack(anchor="w")
        txt = read_only_text(self, detail, font=("Consolas", 10))
        txt.pack(fill="both", expand=True, padx=12, pady=4)
        btns = ttk.Frame(self)
        btns.pack(pady=8)
        if backup_path and on_restore:
            def restore():
                if not messagebox.askyesno(
                        "还原", "确定用写入前的备份覆盖当前配置吗？\n\n%s"
                        % backup_path, parent=self):
                    return
                self.destroy()
                on_restore(backup_path)
            ttk.Button(btns, text="还原到写入前", command=restore).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="left", padx=6)


class ManualPasteDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("手动粘贴模型 ID")
        self.geometry("520x380")
        self.transient(app.root)
        self.grab_set()
        ttk.Label(self, text="每行一个模型 ID（服务商后台复制过来即可）：").pack(
            anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(self)
        txt.pack(fill="both", expand=True, padx=10, pady=4)
        txt.focus_set()
        btns = ttk.Frame(self)
        btns.pack(pady=8)
        ttk.Button(btns, text="添加到表格",
                   command=lambda: self._add(txt)).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=6)

    def _add(self, txt):
        lines = [l.strip() for l in txt.get("1.0", "end").splitlines() if l.strip()]
        self.destroy()
        self.app.add_manual_ids(lines)


class BackupDialog(tk.Toplevel):
    """备份管理：看有哪些备份、还原某一份、打开所在文件夹。"""

    def __init__(self, parent, on_restored=None):
        super().__init__(parent)
        self.on_restored = on_restored
        self.title("备份管理")
        self.geometry("760x420")
        self.transient(parent)
        self.grab_set()
        ttk.Label(self, wraplength=720, justify="left", padding=(12, 10),
                  text="每次写入前工具都会备份一份 models.json，只保留最近 %d 份。"
                       "还原会先把当前配置再备份一次，所以还原本身也是可撤销的。"
                       % store.BACKUP_KEEP).pack(anchor="w")
        cols = ("file", "time", "size", "provs")
        heads = {"file": "备份文件", "time": "时间", "size": "大小",
                 "provs": "含服务商"}
        widths = {"file": 260, "time": 150, "size": 90, "provs": 200}
        frm, self.tv = scrolled_tree(self, cols, heads, widths,
                                     stretch=("provs",), center=("size",), height=10)
        frm.pack(fill="both", expand=True, padx=12, pady=4)
        btns = ttk.Frame(self)
        btns.pack(pady=8)
        ttk.Button(btns, text="还原选中备份", command=self._restore).pack(side="left", padx=6)
        ttk.Button(btns, text="打开所在文件夹", command=self._open_dir).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="left", padx=6)
        self._fill()

    def _fill(self):
        import time as _t
        self.tv.delete(*self.tv.get_children())
        self.files = store.list_backups(CONFIG_PATH)
        for i, f in enumerate(self.files):
            try:
                st = os.stat(f)
                data = json.load(open(f, encoding="utf-8"))
                provs = "、".join(list((data.get("providers") or {}).keys())[:6])
            except Exception:  # noqa: BLE001
                st = None
                provs = "（读不出来）"
            self.tv.insert("", "end", iid="b%d" % i, values=(
                os.path.basename(f),
                _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(st.st_mtime)) if st else "?",
                "%.1f KB" % (st.st_size / 1024) if st else "?", provs))
        if not self.files:
            self.tv.insert("", "end", values=("（还没有备份）", "", "", ""))

    def _sel_file(self):
        sel = self.tv.selection()
        if not sel or not self.files:
            return None
        try:
            return self.files[int(sel[0][1:])]
        except (ValueError, IndexError):
            return None

    def _restore(self):
        f = self._sel_file()
        if not f:
            messagebox.showinfo("提示", "先点选一份备份", parent=self)
            return
        if not messagebox.askyesno(
                "还原", "确定用这份备份覆盖当前配置吗？\n\n%s\n\n"
                        "当前配置会先被备份一次。" % f, parent=self):
            return
        try:
            bak = store.restore_backup(CONFIG_PATH, f)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("还原失败", str(e), parent=self)
            return
        messagebox.showinfo("已还原", "已用备份覆盖当前配置。\n\n"
                            "覆盖前的内容备份在：\n%s" % bak, parent=self)
        self._fill()
        if self.on_restored:
            self.on_restored()

    def _open_dir(self):
        d = os.path.dirname(CONFIG_PATH)
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)  # noqa: S606  Windows 专用
        except Exception:  # noqa: BLE001
            try:
                subprocess.Popen(["explorer", d])
            except Exception as e:  # noqa: BLE001
                messagebox.showinfo("打开失败", "手动去这里看：%s（%s）" % (d, e),
                                    parent=self)
