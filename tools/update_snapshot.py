# -*- coding: utf-8 -*-
"""把 snapshot 文件夹更新成 npm 上最新的 @earendil-works/pi-ai 参数库。

用法：python tools/update_snapshot.py [--dry-run]

工具运行时优先读本机 pi 的实时数据，快照只是没装 pi 时的兜底，
所以这个脚本平时不用手动跑，交给 CI 定期执行即可。
只做两件事：下载官方 npm 包、把 dist/providers/data 里的 json 覆盖进 snapshot。
"""
import argparse
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

REGISTRY = "https://registry.npmjs.org/@earendil-works%2Fpi-ai/latest"
DATA_PREFIX = "package/dist/providers/data/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "snapshot")

sys.path.insert(0, ROOT)


def fetch_latest_meta():
    req = urllib.request.Request(REGISTRY, headers={"User-Agent": "pimct-snapshot"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        meta = json.load(resp)
    return meta["version"], meta["dist"]["tarball"]


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pimct-snapshot"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def extract(tarball_bytes, dest):
    got = 0
    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.startswith(DATA_PREFIX):
                continue
            name = os.path.basename(m.name)
            if not name.endswith(".json") or name.startswith("."):
                continue      # .manifest.json 之类的辅助文件不需要
            f = tf.extractfile(m)
            if f is None:
                continue
            data = f.read()
            json.loads(data.decode("utf-8"))   # 坏文件不写进去
            with open(os.path.join(dest, name), "wb") as out:
                out.write(data)
            got += 1
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只看最新版本号，不动文件")
    args = ap.parse_args()

    version, tarball = fetch_latest_meta()
    print("npm 上最新的 pi-ai 版本：%s" % version)
    if args.dry_run:
        return 0

    old = {}
    if os.path.isfile(os.path.join(SNAPSHOT, "VERSION")):
        with open(os.path.join(SNAPSHOT, "VERSION"), encoding="utf-8") as f:
            old = json.load(f)
    if old.get("piAiVersion") == version:
        print("快照已经是这个版本，不用改")
        return 0

    tmp = tempfile.mkdtemp(prefix="pimct_snap_")
    try:
        n = extract(download(tarball), tmp)
        if n == 0:
            print("这个包里没找到 providers/data，放弃更新", file=sys.stderr)
            return 1
        os.makedirs(SNAPSHOT, exist_ok=True)
        for f in os.listdir(SNAPSHOT):
            if f.endswith(".json") and f != "VERSION":
                os.remove(os.path.join(SNAPSHOT, f))
        for f in os.listdir(tmp):
            shutil.copy2(os.path.join(tmp, f), os.path.join(SNAPSHOT, f))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    import catalog
    meta = catalog.snapshot_write_meta(SNAPSHOT, version)
    cat = catalog.Catalog(prefer_snapshot=True)
    print("已更新 %d 个文件，共 %d 个型号，标记为 %s" % (n, len(cat), meta))
    if len(cat) < 300:
        print("型号数量明显偏少，请人工确认", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
