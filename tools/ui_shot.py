# -*- coding: utf-8 -*-
"""给界面截图用的小工具：起一个填了示例数据的主窗口，截图后退出。

用法：python tools/ui_shot.py 输出文件.png
纯粹为了做界面前后对比，不属于工具功能。
"""
import os
import subprocess
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PS = r'''
Add-Type -AssemblyName System.Drawing,System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Cap {
  [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string n);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint f);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  public struct RECT { public int L,T,R,B; }
}
"@
$h = (Get-Process -Id __PID__).MainWindowHandle
if ($h -eq [IntPtr]::Zero) { "窗口没找到"; exit 1 }
[Cap]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 600
$r = New-Object Cap+RECT
[Cap]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.R - $r.L; $hh = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[Cap]::PrintWindow($h, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc)
$bmp.Save("__OUT__")
"已截图 __OUT__ 尺寸 ${w}x${hh}"
'''


def main():
    out_png = sys.argv[1] if len(sys.argv) > 1 else "ui_shot.png"
    out_png = os.path.abspath(out_png)

    import catalog as catalog_mod
    import matcher
    import store
    import ui.app as app_mod

    app_mod.App.on_fetch = lambda self: None       # 不碰网络
    root = tk.Tk()
    app = app_mod.App(root)
    app.var_name.set("mysvc")
    app.var_url.set("https://relay.example.com/v1")
    app.var_key.set("sk-demo-key-1234567890")

    cat = app.catalog
    demo = ["gpt-5.6", "claude-opus-4-8", "deepseek-v4-flash", "grok-4.6",
            "kimi-k3-cc", "relay-only-model-x"]
    for mid in demo:
        r = matcher.match(cat, mid, top=1)
        best = r[0] if r else None
        app.add_row(mid, best.entry if best else None, best.score if best else 0.0)
    for i, iid in enumerate(list(app.rowdata)):
        app.rowdata[iid].checked = i % 2 == 0
        app.refresh_row(iid)
    if hasattr(app, "_apply_auto_rules"):
        app._apply_auto_rules()
    app._refresh_count()
    app.set_status("示例数据，仅用于界面截图对比")
    root.update()
    root.update_idletasks()

    title = root.title()
    script = PS.replace("__PID__", str(os.getpid())).replace("__OUT__", out_png.replace("\\", "\\\\"))
    fd, ps1 = tempfile.mkstemp(suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(script)

    def shoot():
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", ps1], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            msg = ((r.stdout or "").strip() or (r.stderr or "").strip())
            sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
        finally:
            os.remove(ps1)
            root.after(200, root.destroy)

    root.after(1200, shoot)
    root.mainloop()


if __name__ == "__main__":
    main()
