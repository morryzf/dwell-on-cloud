"""
从 rullerzhou-afk/clawd-on-desk 拉 pet 用的 clawd-*.svg 到 static/pet/。
跟 ensure_frontend() 同一条自愈路：外部资源不进 dwell 仓库,启动时长回来。
容器重建后 static/ 是干净的,靠这个函数拉回。
"""
from __future__ import annotations
import logging
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

REPO_RAW = "https://raw.githubusercontent.com/rullerzhou-afk/clawd-on-desk/main/assets/svg"

# 从 clawd-on-desk 的 assets/svg 目录扫出来的完整 clawd 前缀 SVG 清单
# 前端可能在任何状态下切换,一次拉齐避免以后又碎
CLAWD_SVGS = [
    "clawd-about-hero.svg", "clawd-aegyo-shy.svg",
    "clawd-coffee-hand.svg", "clawd-coffee-head-flip.svg",
    "clawd-collapse-sleep.svg", "clawd-dizzy.svg",
    "clawd-error.svg", "clawd-happy.svg",
    "clawd-headphones-groove.svg",
    "clawd-idle-bubble.svg", "clawd-idle-collapse.svg",
    "clawd-idle-doze.svg", "clawd-idle-follow.svg",
    "clawd-idle-living.svg", "clawd-idle-look.svg",
    "clawd-idle-low-battery.svg", "clawd-idle-reading.svg",
    "clawd-idle-yawn.svg",
    "clawd-mini-alert.svg", "clawd-mini-crabwalk.svg",
    "clawd-mini-enter.svg", "clawd-mini-enter-sleep.svg",
    "clawd-mini-happy.svg", "clawd-mini-idle.svg",
    "clawd-mini-peek.svg", "clawd-mini-sleep.svg",
    "clawd-mini-typing.svg",
    "clawd-notification.svg",
    "clawd-react-annoyed.svg", "clawd-react-double.svg",
    "clawd-react-double-jump.svg", "clawd-react-drag.svg",
    "clawd-react-left.svg", "clawd-react-right.svg",
    "clawd-sleeping.svg", "clawd-static-base.svg", "clawd-wake.svg",
    "clawd-working-building.svg", "clawd-working-carrying.svg",
    "clawd-working-debugger.svg", "clawd-working-juggling.svg",
    "clawd-working-sweeping.svg", "clawd-working-thinking.svg",
    "clawd-working-typing.svg", "clawd-working-typing-boss.svg",
    "clawd-working-ultrathink.svg", "clawd-working-wizard.svg",
]

CRITICAL = "clawd-idle-follow.svg"  # 前端默认状态,少这个就是右下角碎图


def _fetch_one(name: str, dst_dir: Path, timeout: int = 15):
    url = f"{REPO_RAW}/{name}"
    dst = dst_dir / name
    if dst.exists() and dst.stat().st_size > 0:
        return (name, True, "cached")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dwell/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return (name, False, f"http {resp.status}")
            data = resp.read()
        if not data or not data.lstrip().startswith(b"<"):
            return (name, False, "not svg")
        tmp = dst.with_suffix(".svg.tmp")
        tmp.write_bytes(data)
        tmp.rename(dst)  # 原子替换,防写一半崩了留半截文件
        return (name, True, "fetched")
    except Exception as e:
        return (name, False, f"err: {e}")


def ensure_pet_assets(static_dir="static", workers: int = 6) -> dict:
    """
    幂等:已存在的文件不重拉。
    并发:6 个线程一起下,一般 1-3 秒完事。
    容错:某几个失败不影响整体启动;critical 缺失会 log.error。
    """
    dst_dir = Path(static_dir) / "pet"
    dst_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    results = {"fetched": [], "cached": [], "fail": []}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for name, ok, msg in ex.map(lambda n: _fetch_one(n, dst_dir), CLAWD_SVGS):
            if ok and msg == "cached":
                results["cached"].append(name)
            elif ok:
                results["fetched"].append(name)
            else:
                results["fail"].append((name, msg))

    dt = time.monotonic() - t0
    log.info(
        "ensure_pet_assets: %d fetched, %d cached, %d failed, %.2fs",
        len(results["fetched"]), len(results["cached"]), len(results["fail"]), dt,
    )
    for name, msg in results["fail"]:
        log.warning("  pet fail: %s (%s)", name, msg)

    if not (dst_dir / CRITICAL).exists():
        log.error("ensure_pet_assets: critical %s missing, 右下角会碎", CRITICAL)

    return results
