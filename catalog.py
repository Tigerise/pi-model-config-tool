# -*- coding: utf-8 -*-
"""官方模型参数库加载器。

数据来源有两个，优先用本机 pi 安装目录里的实时数据，
找不到时退回随工具打包的快照副本（snapshot 文件夹）。

pi 的安装位置随 npm 前缀、安装方式而变，所以这里按多条候选路径依次探测，
并支持用环境变量 PIMCT_CATALOG_DIR 手动指定。
"""
import json
import os
import shutil
import subprocess
import sys
import glob
import time

ENV_OVERRIDE = "PIMCT_CATALOG_DIR"
PI_PKG = os.path.join("@earendil-works", "pi-coding-agent")
AI_TAIL = os.path.join("@earendil-works", "pi-ai", "dist", "providers", "data")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def snapshot_dirs():
    """快照目录候选（打包后在 _MEIPASS 里，源码运行时在项目文件夹里）。"""
    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "snapshot"))
    dirs.append(os.path.join(_here(), "snapshot"))
    return dirs


def _npm_roots():
    """可能装着全局 npm 包的目录。"""
    roots = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        roots.append(os.path.join(appdata, "npm", "node_modules"))
    # pi 命令所在目录的同级 node_modules（覆盖自定义 npm 前缀、nvm 等情况）
    for exe in ("pi", "npm"):
        p = shutil.which(exe)
        if p:
            d = os.path.dirname(os.path.abspath(p))
            roots.append(os.path.join(d, "node_modules"))
            roots.append(os.path.join(d, "..", "lib", "node_modules"))
    prefix = os.environ.get("npm_config_prefix") or os.environ.get("NPM_PREFIX")
    if prefix:
        roots.append(os.path.join(prefix, "node_modules"))
        roots.append(os.path.join(prefix, "lib", "node_modules"))
    out, seen = [], set()
    for r in roots:
        r = os.path.normpath(r)
        if r not in seen and os.path.isdir(r):
            seen.add(r)
            out.append(r)
    return out


def _npm_root_global():
    """最后手段：问 npm 自己要全局目录（慢，只在前面都没命中时用）。"""
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        r = subprocess.run([npm, "root", "-g"], capture_output=True, text=True,
                           timeout=20, encoding="utf-8", errors="replace",
                           creationflags=_NO_WINDOW, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (r.stdout or "").strip().splitlines()
    return out[-1].strip() if out else None


def _live_candidates(deep=False):
    """本机 pi 安装目录里的实时参数库候选路径。"""
    cands = []
    for root in _npm_roots():
        # 嵌套安装（pi-ai 在 pi-coding-agent 自己的 node_modules 里）
        cands.append(os.path.join(root, PI_PKG, "node_modules", AI_TAIL))
        # 扁平安装（npm 把 pi-ai 提到了顶层）
        cands.append(os.path.join(root, AI_TAIL))
    if deep:
        root = _npm_root_global()
        if root:
            cands.append(os.path.join(root, PI_PKG, "node_modules", AI_TAIL))
            cands.append(os.path.join(root, AI_TAIL))
        # 兜底：在 npm 目录下有限深度搜一遍
        for r in _npm_roots():
            pat = os.path.join(r, "**", "pi-ai", "dist", "providers", "data")
            try:
                cands.extend(glob.glob(pat, recursive=True))
            except OSError:
                pass
    return cands


def _candidate_dirs(prefer_snapshot=False, deep=False):
    dirs = []
    env = os.environ.get(ENV_OVERRIDE, "").strip()
    if env:
        dirs.append(env)
    if prefer_snapshot:
        dirs.extend(snapshot_dirs())
        dirs.extend(_live_candidates(deep))
    else:
        dirs.extend(_live_candidates(deep))
        dirs.extend(snapshot_dirs())
    out, seen = [], set()
    for d in dirs:
        d = os.path.normpath(d)
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def find_pi_dist():
    """找到 pi 本体的 dist 目录（用于调用 pi 自带的配置校验器）。没找到返回 None。"""
    for root in _npm_roots():
        d = os.path.join(root, PI_PKG, "dist")
        if os.path.isfile(os.path.join(d, "core", "model-config.js")):
            return d
    root = _npm_root_global()
    if root:
        d = os.path.join(root, PI_PKG, "dist")
        if os.path.isfile(os.path.join(d, "core", "model-config.js")):
            return d
    return None


def _pkg_version(data_dir):
    """从 pi-ai 的 package.json 里读版本号（实时数据才有）。"""
    d = data_dir
    for _ in range(5):
        d = os.path.dirname(d)
        pj = os.path.join(d, "package.json")
        if os.path.isfile(pj):
            try:
                with open(pj, encoding="utf-8") as f:
                    return json.load(f).get("version")
            except Exception:  # noqa: BLE001
                return None
    return None


def _snapshot_meta(data_dir):
    """读快照目录里的 VERSION 文件（CI 自动更新时写入）。"""
    f = os.path.join(data_dir, "VERSION")
    if not os.path.isfile(f):
        return {}
    try:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


class CatalogEntry:
    __slots__ = ("vendor", "api", "model")

    def __init__(self, vendor, api, model):
        self.vendor = vendor      # 厂商文件名，例如 xai
        self.api = api            # 该厂商此模型用的接口类型
        self.model = model        # 完整官方模型参数字典

    @property
    def mid(self):
        return self.model.get("id", "")

    def display(self):
        name = self.model.get("name") or self.mid
        return "%s（%s）" % (name, self.vendor)


class Catalog:
    def __init__(self, prefer_snapshot=False):
        self.entries = []
        self.source_dir = None
        self.is_snapshot = False
        self.version = None
        self.updated_at = None
        self._norm_index = None
        self.load(prefer_snapshot=prefer_snapshot)

    def load(self, prefer_snapshot=False):
        last_err = None
        # 先按快路径找，全都没命中再花时间问 npm、递归搜
        for deep in (False, True):
            for d in _candidate_dirs(prefer_snapshot=prefer_snapshot, deep=deep):
                try:
                    entries = self._read_dir(d)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    continue
                if entries:
                    self.entries = entries
                    self.source_dir = d
                    self._norm_index = None
                    self.is_snapshot = os.path.basename(d).lower() == "snapshot"
                    if self.is_snapshot:
                        meta = _snapshot_meta(d)
                        self.version = meta.get("piAiVersion")
                        self.updated_at = meta.get("updatedAt")
                    else:
                        self.version = _pkg_version(d)
                    return
        raise RuntimeError("找不到可用的官方参数库：%r" % last_err)

    @staticmethod
    def _read_dir(d):
        entries = []
        for f in sorted(glob.glob(os.path.join(d, "*.json"))):
            vendor = os.path.splitext(os.path.basename(f))[0]
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            for api, models in data.items():
                if not isinstance(models, dict):
                    continue
                for _mid, m in models.items():
                    if isinstance(m, dict) and m.get("id"):
                        entries.append(CatalogEntry(vendor, api, m))
        return entries

    def norm_index(self):
        """归一化 ID 到条目列表的索引，只建一次（matcher 每次调用都要用）。"""
        if self._norm_index is None:
            import matcher
            idx = {}
            for e in self.entries:
                idx.setdefault(matcher.normalize(e.mid), []).append(e)
            self._norm_index = idx
        return self._norm_index

    def source_label(self):
        """给界面看的一句话来源说明。"""
        if self.is_snapshot:
            extra = []
            if self.version:
                extra.append("对应 pi-ai %s" % self.version)
            if self.updated_at:
                extra.append("更新于 %s" % self.updated_at)
            tail = "（%s）" % "，".join(extra) if extra else ""
            return "内置快照%s，装了 pi 会自动改用实时数据" % tail
        return "本机 pi 实时数据%s" % (" pi-ai %s" % self.version
                                      if self.version else "")

    def __len__(self):
        return len(self.entries)


def snapshot_write_meta(dirpath, pi_ai_version):
    """CI 更新快照时写入版本信息。"""
    meta = {"piAiVersion": pi_ai_version,
            "updatedAt": time.strftime("%Y-%m-%d")}
    with open(os.path.join(dirpath, "VERSION"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return meta


def describe_thinking(level_map, reasoning):
    """把思考等级信息转成人能看懂的一句话。

    level_map 里只放明确表态的档位：值为字符串表示可用，值为 None 表示不可用；
    没写进来的档位视为交给 pi 按厂商惯例处理（显示成默认）。
    """
    if not reasoning:
        return "不支持思考"
    m = level_map
    levels = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
    zh = {"off": "关", "minimal": "极低", "low": "低", "medium": "中",
          "high": "高", "xhigh": "超高", "max": "最大"}
    if not m:
        return "默认映射（由 pi 决定）"
    ok = [zh[k] for k in levels if k in m and m[k] is not None]
    no = [zh[k] for k in levels if k in m and m[k] is None]
    keep = [zh[k] for k in levels if k not in m]
    parts = []
    if ok:
        parts.append("可用：" + "、".join(ok))
    if no:
        parts.append("不可用：" + "、".join(no))
    if keep and not ok and not no:
        return "默认映射（由 pi 决定）"
    if keep:
        parts.append("默认：" + "、".join(keep))
    return "；".join(parts) if parts else "默认映射（由 pi 决定）"
