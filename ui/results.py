# -*- coding: utf-8 -*-
"""实测结果窗口：每个模型每个测试项一行，颜色区分结论。"""
import queue
import tkinter as tk
from tkinter import ttk, messagebox

import tester

from .common import scrolled_tree


class TestResultsWindow(tk.Toplevel):
    VERDICT_ZH = {tester.PASS: "通过", tester.WARN: "警告",
                  tester.FAIL: "失败", tester.SKIP: "跳过"}

    def __init__(self, parent, total, on_done=None, on_apply_fixes=None):
        super().__init__(parent)
        self.on_done = on_done
        self.on_apply_fixes = on_apply_fixes
        self.title("实测结果")
        self.geometry("1000x600")
        self.resizable(True, True)
        self.transient(parent)
        self.q = queue.Queue()
        self.fixes = {}          # 模型 ID -> [可回写的结论]

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(8, 2))
        self.var_prog = tk.StringVar(value="准备测试……")
        ttk.Label(top, textvariable=self.var_prog,
                  foreground="#155a8a").pack(side="left")
        ttk.Label(top, foreground="#888",
                  text="上下文为抽查，不代表标称全长；测试会消耗少量 tokens"
                  ).pack(side="right")

        cols = ("model", "item", "verdict", "latency", "detail")
        heads = {"model": "模型", "item": "测试项", "verdict": "结论",
                 "latency": "延迟", "detail": "说明"}
        widths = {"model": 200, "item": 90, "verdict": 60, "latency": 80,
                  "detail": 460}
        frm, self.tv = scrolled_tree(self, cols, heads, widths,
                                     stretch=("detail",),
                                     center=("verdict", "latency"), height=18)
        frm.pack(fill="both", expand=True, padx=10, pady=4)
        self.tv.tag_configure("pass", foreground="#2e7d32")
        self.tv.tag_configure("warn", foreground="#b26a00")
        self.tv.tag_configure("fail", foreground="#c62828")
        self.tv.tag_configure("skip", foreground="#888888")
        self.tv.tag_configure("overall", background="#f0f4f8")

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(2, 8), padx=10)
        self.var_fix = tk.StringVar(value="暂无可回写的结论")
        ttk.Label(bar, textvariable=self.var_fix,
                  foreground="#b26a00").pack(side="left")
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")
        self.btn_fix = ttk.Button(bar, text="把实测结论应用到配置",
                                  command=self._apply_fixes, state="disabled")
        self.btn_fix.pack(side="right", padx=6)

        self.reset(total)
        self.after(150, self._poll)

    def reset(self, total):
        self.tv.delete(*self.tv.get_children())
        self.fixes = {}
        self.var_prog.set("准备测试（共 %d 个模型）……" % total)
        self._refresh_fix_state()

    def _refresh_fix_state(self):
        n = sum(len(v) for v in self.fixes.values())
        if n and self.on_apply_fixes:
            self.var_fix.set("有 %d 条实测结论可以回写到配置（涉及 %d 个模型）"
                             % (n, len(self.fixes)))
            self.btn_fix.config(state="normal")
        else:
            self.var_fix.set("暂无可回写的结论")
            self.btn_fix.config(state="disabled")

    def _apply_fixes(self):
        if not self.on_apply_fixes or not self.fixes:
            return
        lines = []
        for mid, fixes in sorted(self.fixes.items()):
            for fx in fixes:
                lines.append("· %s：%s" % (mid, tester.fix_text(fx)))
        if not messagebox.askyesno(
                "应用实测结论",
                "会把下面的结论写成待应用的改动（还要在管理窗口点“应用更改”"
                "才真正写盘）：\n\n" + "\n".join(lines[:15])
                + ("\n……共 %d 条" % len(lines) if len(lines) > 15 else ""),
                parent=self):
            return
        self.on_apply_fixes(dict(self.fixes))
        self.fixes = {}
        self._refresh_fix_state()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "result":
                    _, mid, label, verdict, lat, detail, fixes = msg
                    lat_txt = "%.0f ms" % lat if lat else ""
                    iid = self.tv.insert("", "end", values=(
                        mid, label, self.VERDICT_ZH.get(verdict, verdict),
                        lat_txt, detail), tags=(verdict,))
                    self.tv.see(iid)
                    if fixes:
                        cur = self.fixes.setdefault(mid, [])
                        for fx in fixes:
                            if fx not in cur:
                                cur.append(fx)
                        self._refresh_fix_state()
                elif kind == "overall":
                    _, mid, ov = msg
                    iid = self.tv.insert("", "end", values=(
                        mid, "结论", self.VERDICT_ZH.get(ov, ov), "", ""),
                        tags=("overall", ov))
                    self.tv.see(iid)
                elif kind == "progress":
                    self.var_prog.set(msg[1])
                elif kind == "done":
                    self.var_prog.set(msg[1])
                    if self.on_done:
                        try:
                            self.on_done()
                        except Exception:  # noqa: BLE001
                            pass
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)
