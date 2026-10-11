import os
import time
from pathlib import Path

_prev_cpu = None


def _cpu_percent():
    global _prev_cpu
    line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    nums = [int(part) for part in line.split()[1:8]]
    idle = nums[3] + nums[4]
    total = sum(nums)
    prev = _prev_cpu
    _prev_cpu = (total, idle)
    if prev is None:
        time.sleep(0.05)
        return _cpu_percent()
    delta = total - prev[0]
    if delta <= 0:
        return None
    busy = delta - (idle - prev[1])
    return round(max(0.0, min(100.0, 100.0 * busy / delta)), 1)


def _meminfo():
    info = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0]) * 1024
    return info


def _disk(path):
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    total = stats.f_frsize * stats.f_blocks
    avail = stats.f_frsize * stats.f_bavail
    used = total - (stats.f_frsize * stats.f_bfree)
    percent = round(100.0 * used / total, 1) if total else 0
    return {"total": total, "used": used, "avail": avail, "percent": percent}


def _recorder_running():
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"cctv-record.sh" in command:
            return True
    return False


def _latest_recording():
    root = Path("/mnt/cctv")
    if not root.is_dir():
        return None
    newest = None
    try:
        days = list(root.iterdir())
    except OSError:
        return None
    for day in days:
        if not day.is_dir() or day.name == "lost+found":
            continue
        try:
            clips = list(day.iterdir())
        except OSError:
            continue
        for clip in clips:
            if clip.suffix != ".mp4":
                continue
            try:
                stats = clip.stat()
            except OSError:
                continue
            if newest is None or stats.st_mtime > newest[0]:
                newest = (stats.st_mtime, stats.st_size, day.name + "/" + clip.name)
    if newest is None:
        return None
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest[0])),
        "age_seconds": max(0, int(time.time() - newest[0])),
        "size": newest[1],
        "name": newest[2],
    }


def snapshot():
    try:
        load = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        memory = _meminfo()
        cpu = _cpu_percent()
    except OSError:
        return {"available": False}
    total = memory.get("MemTotal", 0)
    available = memory.get("MemAvailable", 0)
    swap_total = memory.get("SwapTotal", 0)
    swap_free = memory.get("SwapFree", 0)
    recording = _latest_recording()
    running = _recorder_running()
    fresh = bool(recording and recording["age_seconds"] < 180)
    if running and fresh:
        state = "merekam"
    elif running:
        state = "proses hidup, berkas terakhir tidak bertambah"
    else:
        state = "berhenti"
    return {
        "available": True,
        "cpu_percent": cpu,
        "cores": os.cpu_count() or 1,
        "load": [float(load[0]), float(load[1]), float(load[2])],
        "uptime_seconds": int(uptime),
        "memory": {"total": total, "available": available, "used": max(0, total - available)},
        "swap": {"total": swap_total, "free": swap_free, "used": max(0, swap_total - swap_free)},
        "root_disk": _disk("/"),
        "cctv_disk": _disk("/mnt/cctv"),
        "cctv": {
            "running": running,
            "state": state,
            "retention_days": 30,
            "latest": recording,
        },
    }
