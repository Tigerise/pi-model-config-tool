# -*- coding: utf-8 -*-
"""图形界面（tkinter 标准库实现）。

拆成几个模块：common 公用常量、app 主窗口、dialogs 各类对话框、
manage 管理窗口、results 实测结果窗口。
"""
import tkinter as tk
from tkinter import messagebox

from .app import App

__all__ = ["App", "run"]


def run():
    root = tk.Tk()
    try:
        App(root)
    except Exception as e:  # noqa: BLE001
        messagebox.showerror("启动失败", str(e))
        return 1
    root.mainloop()
    return 0
