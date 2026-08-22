# -*- coding: utf-8 -*-
"""界面各部分共用的常量和小工具。"""
import tkinter as tk
from tkinter import ttk

import fetcher
import store

CONFIG_PATH = store.DEFAULT_CONFIG_PATH
API_LABEL_LIST = list(fetcher.API_LABELS.keys())

CHECKED = "☑"
UNCHECKED = "☐"

API_ZH = {fetcher.API_ANTHROPIC: "Claude格式",
          fetcher.API_GOOGLE: "Gemini格式",
          fetcher.API_OPENAI: "OpenAI格式"}

FILTER_ALL = "全部"
FILTER_CHECKED = "只看已勾选"
FILTER_MATCHED = "只看已识别"
FILTER_UNMATCHED = "只看未识别"
FILTER_CONFIGURED = "只看已配置"
FILTER_LIST = [FILTER_ALL, FILTER_CHECKED, FILTER_MATCHED,
               FILTER_UNMATCHED, FILTER_CONFIGURED]

HEADER_HINT = ('留空表示不加自定义请求头。需要伪装时按 JSON 填，例如 '
               '{"User-Agent": "claude-cli/2.1.0 (external, cli)", "x-app": "cli"}')


def api_label(api):
    return {fetcher.API_OPENAI: "OpenAI 兼容",
            fetcher.API_ANTHROPIC: "Claude 格式",
            fetcher.API_GOOGLE: "Gemini 格式"}.get(api, "跟随服务商")


def api_from_label(label):
    return {"OpenAI 兼容": fetcher.API_OPENAI,
            "Claude 格式": fetcher.API_ANTHROPIC,
            "Gemini 格式": fetcher.API_GOOGLE}.get(label)


def fmt_num(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return "{:,}".format(n) if n else "?"


def scrolled_tree(parent, cols, heads, widths, stretch=(), center=(), height=14,
                  on_heading=None):
    """建一个带竖滚动条的表格，返回 (frame, tree)。"""
    frm = ttk.Frame(parent)
    tree = ttk.Treeview(frm, columns=cols, show="headings", height=height)
    for c in cols:
        if on_heading is not None:
            tree.heading(c, text=heads[c],
                         command=lambda cc=c: on_heading(cc))
        else:
            tree.heading(c, text=heads[c])
        tree.column(c, width=widths[c], anchor=("center" if c in center else "w"),
                    stretch=(c in stretch))
    vs = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vs.set)
    tree.pack(side="left", fill="both", expand=True)
    vs.pack(side="left", fill="y")
    return frm, tree


def read_only_text(parent, content, font=None):
    txt = tk.Text(parent, wrap="word", font=font)
    txt.insert("1.0", content)
    txt.config(state="disabled")
    return txt
