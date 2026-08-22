# -*- coding: utf-8 -*-
"""管理窗口：查看、修改、批量删除已有服务商与模型，以及逐模型实测。

标记和修改都只暂存在内存里，点“应用更改”才真正备份并写盘。
"""
import copy
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk, messagebox

import store
import tester
import validator

from .common import CHECKED, UNCHECKED, CONFIG_PATH, fmt_num, scrolled_tree, \
    read_only_text
from .dialogs import BackupDialog, EditDialog
from .results import TestResultsWindow


class ManagementDialog(tk.Toplevel):

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("管理已有服务商与模型")
        self.geometry("1120x720")
        self.minsize(940, 560)
        self.resizable(True, True)
        self.transient(app.root)
        self.grab_set()

        self.p_mark = set()     # 待删除的服务商名
        self.m_mark = {}        # {服务商名: set(模型id)}
        self.m_edit = {}        # {服务商名: {模型id: 改过的模型字典}}
        self.cur = None         # 当前正在右侧查看的服务商
        self.prov_order = []
        self.miid = {}          # 右侧表格 iid -> 模型 id
        self._test_thread = None
        self._trw = None
        self._stop_flag = None

        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(10, 0))
        ttk.Label(top, foreground="#555", wraplength=1060, justify="left",
                  text="左边点服务商名字看它下面的模型；点行首方格是把整个服务商标记为删除。"
                       "右边勾选模型是把这些模型删掉，双击模型可以直接改参数。"
                       "所有标记和修改只是暂存，点底部“应用更改”才真正写入，"
                       "写入前自动备份并做合法性自检。").pack(anchor="w")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=6)
        body.columnconfigure(0, weight=5)
        body.columnconfigure(1, weight=7)
        body.rowconfigure(0, weight=1)

        # 左侧：服务商列表
        lf = ttk.LabelFrame(body, text="服务商（点行首方格标记整包删除）")
        lf.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        pcols = ("sel", "name", "api", "cnt", "url")
        pheads = {"sel": "删", "name": "名称", "api": "格式", "cnt": "模型数",
                  "url": "地址"}
        pwidth = {"sel": 36, "name": 110, "api": 130, "cnt": 60, "url": 220}
        pfrm, self.ptv = scrolled_tree(lf, pcols, pheads, pwidth,
                                       stretch=("url",), center=("sel", "cnt"),
                                       height=16)
        pfrm.pack(fill="both", expand=True)
        self.ptv.tag_configure("del", foreground="#c62828")
        self.ptv.bind("<Button-1>", self._on_provider_click)

        # 右侧：当前服务商的模型
        rf = ttk.LabelFrame(body, text="选中服务商的模型（点行首方格标记删除，双击改参数）")
        rf.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        mcols = ("sel", "id", "name", "ctx", "maxt", "img", "state")
        mheads = {"sel": "删", "id": "模型 ID", "name": "显示名", "ctx": "上下文",
                  "maxt": "最大输出", "img": "看图", "state": "状态"}
        mwidth = {"sel": 36, "id": 200, "name": 130, "ctx": 80, "maxt": 80,
                  "img": 46, "state": 70}
        # 先占位底部按钮栏，再放表格，保证按钮栏永远可见
        self.mbar = ttk.Frame(rf)
        self.mbar.pack(side="bottom", fill="x", pady=3)
        ttk.Button(self.mbar, text="本页全标为删除",
                   command=lambda: self._mark_models(True)).pack(side="left", padx=3)
        ttk.Button(self.mbar, text="清除本页标记",
                   command=lambda: self._mark_models(False)).pack(side="left")
        ttk.Button(self.mbar, text="改选中模型参数",
                   command=self._edit_selected).pack(side="left", padx=3)
        mfrm, self.mtv = scrolled_tree(rf, mcols, mheads, mwidth,
                                       stretch=("id", "name"),
                                       center=("sel", "ctx", "maxt", "img", "state"),
                                       height=16)
        mfrm.pack(fill="both", expand=True)
        self.mtv.tag_configure("del", foreground="#c62828")
        self.mtv.tag_configure("edited", foreground="#155a8a")
        self.mtv.bind("<Button-1>", self._on_model_click)
        self.mtv.bind("<Double-1>", self._on_model_double)

        # 模型实测控制条（独立整行，窗口窄也不会被遮挡）
        tbar = ttk.LabelFrame(self, text="模型实测（会消耗少量 tokens）")
        tbar.pack(fill="x", padx=12, pady=(2, 2))
        line1 = ttk.Frame(tbar)
        line1.pack(fill="x", padx=4, pady=(4, 0))
        self.btn_t_all = ttk.Button(line1, text="测试本服务商全部模型",
                                    command=self._test_all)
        self.btn_t_all.pack(side="left")
        self.btn_t_sel = ttk.Button(line1, text="测试选中行（可 Ctrl 多选）",
                                    command=self._test_selected)
        self.btn_t_sel.pack(side="left", padx=4)
        self.btn_t_stop = ttk.Button(line1, text="停止测试", command=self._test_stop,
                                     state="disabled")
        self.btn_t_stop.pack(side="left")
        ttk.Label(line1, text="间隔：").pack(side="left", padx=(12, 0))
        self.var_gap = tk.IntVar(value=10)
        cb_gap = ttk.Combobox(line1, width=4, state="readonly",
                              textvariable=self.var_gap,
                              values=(0, 5, 10, 15, 20, 30))
        cb_gap.pack(side="left")
        ttk.Label(line1, text="秒").pack(side="left")
        ttk.Label(line1, text="并发：").pack(side="left", padx=(12, 0))
        self.var_conc = tk.IntVar(value=1)
        cb_conc = ttk.Combobox(line1, width=3, state="readonly",
                               textvariable=self.var_conc,
                               values=(1, 2, 3, 4))
        cb_conc.pack(side="left")
        self.var_pace = tk.StringVar(value="")
        ttk.Label(line1, textvariable=self.var_pace,
                  foreground="#b26a00").pack(side="left", padx=8)
        for w in (cb_gap, cb_conc):
            w.bind("<<ComboboxSelected>>", lambda _e: self._refresh_pace())

        line2 = ttk.Frame(tbar)
        line2.pack(fill="x", padx=4, pady=(2, 4))
        self.var_t_image = tk.BooleanVar(value=True)
        self.var_t_think = tk.BooleanVar(value=True)
        self.var_ctx_test = tk.BooleanVar(value=True)
        ttk.Checkbutton(line2, text="测图片识别", variable=self.var_t_image).pack(side="left")
        ttk.Checkbutton(line2, text="测思考能力", variable=self.var_t_think).pack(side="left", padx=8)
        ttk.Checkbutton(line2, text="上下文抽查（约 4000 tokens 输入/模型）",
                        variable=self.var_ctx_test).pack(side="left")
        ttk.Label(line2, foreground="#888", wraplength=700, justify="left",
                  text="有的中转会反探测：短时间内连测多个模型可能封 IP。默认串行加 10 秒间隔就是为这个，赶时间再调并发。"
                  ).pack(side="left", padx=8)

        # 底部
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=12, pady=(2, 10))
        self.var_pending = tk.StringVar(value="没有待应用的更改")
        ttk.Label(bottom, textvariable=self.var_pending,
                  foreground="#b26a00").pack(side="left")
        ttk.Button(bottom, text="备份管理",
                   command=self._backups).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="重新加载", command=self._reload).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="关闭", command=self.destroy).pack(side="right")
        self.btn_apply = ttk.Button(bottom, text="应用更改", command=self._apply,
                                    state="disabled")
        self.btn_apply.pack(side="right", padx=6)

        self._load()

    # ---------- 数据 ----------

    def _load(self):
        try:
            self.cfg = store.load_config(CONFIG_PATH)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("读取失败", str(e), parent=self)
            self.destroy()
            return
        self.prov_order = list(self.cfg["providers"].keys())
        if self.cur not in self.prov_order:
            self.cur = None
        self._fill_providers()
        self._fill_models()
        self._refresh_pending()

    def _reload(self):
        if self.p_mark or self.m_mark or self.m_edit:
            if not messagebox.askyesno("重新加载",
                                       "会丢弃当前所有暂存的标记和修改，继续吗？",
                                       parent=self):
                return
        self.p_mark.clear()
        self.m_mark.clear()
        self.m_edit.clear()
        self._load()

    def _backups(self):
        BackupDialog(self, on_restored=self._reload)

    def _fill_providers(self):
        self.ptv.delete(*self.ptv.get_children())
        for name in self.prov_order:
            p = self.cfg["providers"][name]
            models = p.get("models") or []
            url = p.get("baseUrl", "")
            if len(url) > 34:
                url = url[:32] + "…"
            vals = (CHECKED if name in self.p_mark else UNCHECKED, name,
                    p.get("api", "?"), len(models), url)
            iid = self.ptv.insert("", "end", iid=name, values=vals)
            if name in self.p_mark:
                self.ptv.item(iid, tags=("del",))
            if name == self.cur:
                self.ptv.selection_set(iid)

    def _fill_models(self):
        self.mtv.delete(*self.mtv.get_children())
        self.miid = {}
        if not self.cur or self.cur not in self.cfg["providers"]:
            return
        marked = self.m_mark.get(self.cur, set())
        edits = self.m_edit.get(self.cur, {})
        for i, m in enumerate(self._cur_models()):
            mid = m.get("id", "?")
            state = []
            if mid in marked:
                state.append("待删")
            if mid in edits:
                state.append("已改")
            vals = (CHECKED if mid in marked else UNCHECKED, mid,
                    m.get("name", ""), fmt_num(m.get("contextWindow")),
                    fmt_num(m.get("maxTokens")),
                    "能" if "image" in (m.get("input") or []) else "不能",
                    "·".join(state))
            iid = "m%d" % i
            self.miid[iid] = mid
            self.mtv.insert("", "end", iid=iid, values=vals)
            if mid in marked:
                self.mtv.item(iid, tags=("del",))
            elif mid in edits:
                self.mtv.item(iid, tags=("edited",))

    def _cur_models(self):
        """当前服务商的模型（已把暂存的修改套上去，界面和测试都看改后的）。"""
        if not self.cur or self.cur not in self.cfg["providers"]:
            return []
        ms = self.cfg["providers"][self.cur].get("models") or []
        edits = self.m_edit.get(self.cur, {})
        return [edits.get(m.get("id"), m) for m in ms]

    def _model_by_id(self, mid):
        for m in self._cur_models():
            if m.get("id") == mid:
                return m
        return None

    # ---------- 交互 ----------

    def _on_provider_click(self, ev):
        if self.ptv.identify("region", ev.x, ev.y) != "cell":
            return
        name = self.ptv.identify_row(ev.y)
        if not name:
            return
        col = self.ptv.identify_column(ev.x)
        if col == "#1":
            if name in self.p_mark:
                self.p_mark.discard(name)
            else:
                self.p_mark.add(name)
            self._fill_providers()
            self._refresh_pending()
        elif name != self.cur:
            self.cur = name
            self._fill_providers()
            self._fill_models()
            self._refresh_pace()

    def _on_model_click(self, ev):
        if self.mtv.identify("region", ev.x, ev.y) != "cell" or not self.cur:
            return
        iid = self.mtv.identify_row(ev.y)
        if not iid:
            return
        mid = self.miid.get(iid)
        if self.mtv.identify_column(ev.x) == "#1" and mid:
            marks = self.m_mark.setdefault(self.cur, set())
            if mid in marks:
                marks.discard(mid)
            else:
                marks.add(mid)
            if not marks:
                self.m_mark.pop(self.cur, None)
            self._fill_models()
            self._refresh_pending()

    def _mark_models(self, flag):
        if not self.cur:
            return
        if flag:
            self.m_mark[self.cur] = {m.get("id", "?") for m in self._cur_models()}
        else:
            self.m_mark.pop(self.cur, None)
        self._fill_models()
        self._refresh_pending()

    # ---------- 改参数 ----------

    def _on_model_double(self, ev):
        if self.mtv.identify("region", ev.x, ev.y) != "cell":
            return
        if self.mtv.identify_column(ev.x) == "#1":
            return
        iid = self.mtv.identify_row(ev.y)
        if iid:
            self._edit_model(self.miid.get(iid))

    def _edit_selected(self):
        sel = self.mtv.selection()
        if not sel:
            messagebox.showinfo("提示", "先在右边点选一个模型", parent=self)
            return
        self._edit_model(self.miid.get(sel[0]))

    def _edit_model(self, mid):
        if not mid or not self.cur:
            return
        m = self._model_by_id(mid)
        if m is None:
            return
        row = store.row_from_config(copy.deepcopy(m))
        prefer = self.cfg["providers"][self.cur].get("api")
        EditDialog(self, self.app.catalog, row, prefer_api=prefer,
                   on_ok=self._save_edit, title_prefix="修改已配置模型")

    def _save_edit(self, row):
        try:
            newm = store.build_model_dict(row)
        except ValueError as e:
            messagebox.showwarning("参数有误", str(e), parent=self)
            return
        self.m_edit.setdefault(self.cur, {})[row.mid] = newm
        self._fill_models()
        self._refresh_pending()

    # ---------- 实测 ----------

    def _test_all(self):
        self._start_tests([m.get("id") for m in self._cur_models()])

    def _test_selected(self):
        mids = [self.miid.get(iid) for iid in self.mtv.selection()]
        self._start_tests([m for m in mids if m])

    def _test_stop(self):
        if self._stop_flag:
            self._stop_flag.set()

    def _set_test_buttons(self, running):
        st = "disabled" if running else "normal"
        self.btn_t_all.config(state=st)
        self.btn_t_sel.config(state=st)
        self.btn_t_stop.config(state="normal" if running else "disabled")

    def _ensure_results_window(self, total):
        if self._trw is not None and self._trw.winfo_exists():
            self._trw.reset(total)
            return self._trw
        self._trw = TestResultsWindow(
            self, total, on_done=lambda: self._set_test_buttons(False),
            on_apply_fixes=self._apply_fixes)
        return self._trw

    def _refresh_pace(self):
        gap = int(self.var_gap.get() or 0)
        conc = max(1, int(self.var_conc.get() or 1))
        n = len(self._cur_models())
        msg = ""
        if conc > 1:
            msg = "并发 %d 可能触发中转反探测" % conc
        elif n and gap:
            est = (n - 1) * gap
            msg = "串行加 %d 秒间隔，%d 个模型光等待就约 %d 分 %d 秒" % (
                gap, n, est // 60, est % 60)
        self.var_pace.set(msg)

    def _start_tests(self, mids):
        if self._test_thread and self._test_thread.is_alive():
            messagebox.showinfo("提示", "上一轮测试还在进行中", parent=self)
            return
        if not self.cur or self.cur not in self.cfg["providers"]:
            messagebox.showinfo("提示", "先在左边点选一个服务商", parent=self)
            return
        mids = [m for m in mids if m]
        if not mids:
            messagebox.showinfo("提示", "没有可测试的模型", parent=self)
            return
        prov = self.cfg["providers"][self.cur]
        mmap = {m.get("id"): m for m in self._cur_models()}
        objs = [mmap[m] for m in mids if m in mmap]
        if not objs:
            return
        opts = {"image": self.var_t_image.get(),
                "thinking": self.var_t_think.get(),
                "ctx_tokens": 4000 if self.var_ctx_test.get() else 0}
        gap = max(0, int(self.var_gap.get() or 0))
        conc = max(1, int(self.var_conc.get() or 1))
        if conc > 1 and len(objs) > 2 and not messagebox.askyesno(
                "确认并发测试",
                "并发 %d 个模型同时测，部分中转会判定为批量探测并封 IP"
                "（你自己的经验是 60 秒内 5 个不同模型就会被封）。"
                "确定要并发吗？点“否”改回串行加间隔。" % conc, parent=self):
            return
        stop = threading.Event()
        self._stop_flag = stop
        trw = self._ensure_results_window(len(objs))
        self._set_test_buttons(True)
        state = {}
        total = len(objs)

        def run_one(m):
            mid = m.get("id", "?")
            if stop.is_set() or state.get("abort"):
                return mid, []
            try:
                rs = tester.test_model(tester.effective(prov, m), m, opts,
                                       stop=stop, state=state)
            except Exception as e:  # noqa: BLE001
                rs = [{"key": "basic", "label": "测试", "verdict": tester.FAIL,
                       "latency": 0, "detail": "测试过程出错：%s" % e, "fixes": []}]
            return mid, rs

        def report(i, mid, rs):
            trw.q.put(("progress", "已完成 %d/%d，刚测完 %s" % (i, total, mid)))
            for r in rs:
                trw.q.put(("result", mid, r["label"], r["verdict"],
                           r["latency"], r["detail"], r.get("fixes") or []))
            if rs:
                trw.q.put(("overall", mid, tester.overall_of(rs)))

        def worker():
            done = 0
            try:
                if conc == 1:
                    for m in objs:
                        if stop.is_set() or state.get("abort"):
                            break
                        mid, rs = run_one(m)
                        done += 1
                        report(done, mid, rs)
                        if done < total and gap and not state.get("abort"):
                            trw.q.put(("progress",
                                       "已完成 %d/%d，等 %d 秒再测下一个（防中转反探测）"
                                       % (done, total, gap)))
                            if stop.wait(gap):
                                break
                else:
                    with ThreadPoolExecutor(max_workers=conc) as ex:
                        for mid, rs in ex.map(run_one, objs):
                            done += 1
                            report(done, mid, rs)
            finally:
                if state.get("abort"):
                    trw.q.put(("done", "已自动停止：%s。等一段时间再测，"
                               "并把间隔调大" % state["abort"]))
                elif stop.is_set():
                    trw.q.put(("done", "已手动停止，完成 %d/%d" % (done, total)))
                else:
                    trw.q.put(("done", "测试完成，共 %d 个模型" % done))

        self._test_thread = threading.Thread(target=worker, daemon=True)
        self._test_thread.start()

    def _apply_fixes(self, fixes_by_mid):
        """把实测结论变成暂存的模型修改。"""
        if not self.cur:
            return
        applied = []
        for mid, fixes in fixes_by_mid.items():
            m = self._model_by_id(mid)
            if m is None:
                continue
            newm = copy.deepcopy(m)
            prov = self.cfg["providers"].get(self.cur) or {}
            done = store.apply_fixes(newm, fixes,
                                     provider_api=prov.get("api"),
                                     provider_base=prov.get("baseUrl"))
            if done:
                self.m_edit.setdefault(self.cur, {})[mid] = newm
                applied.extend(done)
        self._fill_models()
        self._refresh_pending()
        if applied:
            messagebox.showinfo("已暂存", "已按实测结论改好 %d 处：\n\n%s\n\n"
                                "记得点“应用更改”才会写盘。"
                                % (len(applied), "\n".join("· " + a for a in applied[:15])),
                                parent=self)
        else:
            messagebox.showinfo("无需改动", "实测结论与现有配置一致，没有要改的。",
                                parent=self)

    # ---------- 待应用状态 ----------

    def _refresh_pending(self):
        n_p = len(self.p_mark)
        n_m = sum(len(v) for k, v in self.m_mark.items() if k not in self.p_mark)
        n_e = sum(len(v) for k, v in self.m_edit.items() if k not in self.p_mark)
        parts = []
        if n_p:
            parts.append("待删除服务商 %d 个（%s）"
                         % (n_p, "、".join(sorted(self.p_mark))))
        if n_m:
            parts.append("待删除模型 %d 个" % n_m)
        if n_e:
            parts.append("待修改模型 %d 个" % n_e)
        self.var_pending.set("；".join(parts) if parts else "没有待应用的更改")
        self.btn_apply.config(state="normal" if parts else "disabled")

    # ---------- 应用 ----------

    def _build_preview_text(self, provs):
        lines = []
        if self.p_mark:
            lines.append("将删除的服务商（整个删除）：")
            for name in sorted(self.p_mark):
                cnt = len((provs.get(name) or {}).get("models") or []) \
                    if name in provs else 0
                state = "" if name in provs else "（注意：配置里已不存在，将跳过）"
                lines.append("　· %s（含 %d 个模型）%s" % (name, cnt, state))
        model_lines = []
        for name, ids in sorted(self.m_mark.items()):
            if not ids or name in self.p_mark or name not in provs:
                continue
            ms = provs[name].get("models") or []
            hit = [m.get("id", "?") for m in ms if m.get("id") in ids]
            if not hit:
                continue
            show = "、".join(hit[:8]) + (" 等 %d 个" % len(hit) if len(hit) > 8 else "")
            model_lines.append("　· 从 %s 删除：%s" % (name, show))
        if model_lines:
            lines.append("将删除的模型：")
            lines.extend(model_lines)
        edit_lines = []
        for name, edits in sorted(self.m_edit.items()):
            if name in self.p_mark or name not in provs:
                continue
            keep = [mid for mid in edits
                    if mid not in self.m_mark.get(name, set())]
            if keep:
                edit_lines.append("　· %s：%s" % (name, "、".join(sorted(keep)[:10])))
        if edit_lines:
            lines.append("将更新参数的模型：")
            lines.extend(edit_lines)
        remain = [k for k in provs.keys() if k not in self.p_mark]
        lines.append("")
        lines.append("应用后保留的服务商：%s"
                     % ("、".join(remain) if remain else "（无，配置将被清空！）"))
        empty_warn = [k for k in remain
                      if k in provs and not [m for m in (provs[k].get("models") or [])
                                             if m.get("id") not in self.m_mark.get(k, set())]]
        if empty_warn:
            lines.append("提醒：%s 将不再有任何模型，建议下次直接删掉该服务商。"
                         % "、".join(empty_warn))
        return "\n".join(lines)

    def _apply(self):
        if not self.p_mark and not any(self.m_mark.values()) \
                and not any(self.m_edit.values()):
            return
        try:
            data = store.load_config(CONFIG_PATH)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("读取失败", str(e), parent=self)
            return
        provs = data["providers"]
        baseline = copy.deepcopy(data)
        text = self._build_preview_text(provs)

        win = tk.Toplevel(self)
        win.title("应用更改预览")
        win.geometry("660x520")
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text="确认要执行下面的改动吗？写入前会自动备份并做合法性自检。",
                  padding=(10, 8)).pack(anchor="w")
        read_only_text(win, text).pack(fill="both", expand=True, padx=10)

        def do_apply():
            removed_p, removed_m, edited_m = [], 0, 0
            for name in list(self.p_mark):
                if name in provs:
                    del provs[name]
                    removed_p.append(name)
            for name, edits in self.m_edit.items():
                if name in self.p_mark or name not in provs:
                    continue
                ms = provs[name].get("models") or []
                for i, m in enumerate(ms):
                    new = edits.get(m.get("id"))
                    if new is not None:
                        ms[i] = new
                        edited_m += 1
                provs[name]["models"] = ms
            for name, ids in self.m_mark.items():
                if not ids or name in self.p_mark or name not in provs:
                    continue
                ms = provs[name].get("models") or []
                kept = [m for m in ms if m.get("id") not in ids]
                removed_m += len(ms) - len(kept)
                provs[name]["models"] = kept
            try:
                bak = store.write_config(CONFIG_PATH, data, baseline=baseline)
            except ValueError as e:
                messagebox.showerror("自检没通过，已阻止写入", str(e), parent=win)
                return
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("写入失败", str(e), parent=win)
                return
            win.destroy()
            msg = "已完成：删除服务商 %d 个，删除模型 %d 个，更新模型 %d 个。" % (
                len(removed_p), removed_m, edited_m)
            if bak:
                msg += "\n\n备份文件：%s" % bak
            ok, detail = validator.pi_list_models()
            if ok is False:
                msg += "\n\n注意：pi 读取配置时报错了，建议用备份还原：\n%s" % detail[:400]
            messagebox.showinfo("完成", msg, parent=self)
            self.app.set_status("管理操作完成：删除服务商 %d 个、模型 %d 个、"
                                "更新模型 %d 个。"
                                % (len(removed_p), removed_m, edited_m))
            self.p_mark.clear()
            self.m_mark.clear()
            self.m_edit.clear()
            self._load()

        btns = ttk.Frame(win)
        btns.pack(pady=8)
        ttk.Button(btns, text="确认并写入", command=do_apply).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=6)
