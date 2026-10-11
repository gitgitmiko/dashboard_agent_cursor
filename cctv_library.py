import re
import time
from pathlib import Path

ROOT = Path("/mnt/cctv")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLIP = re.compile(r"^\d{2}-\d{2}-\d{2}\.mp4$")


def _root():
    return ROOT.resolve()


def _day_dir(day):
    if not DAY.fullmatch(day or ""):
        return None
    folder = (ROOT / day).resolve()
    if folder.parent != _root():
        return None
    return folder


def _dates():
    root = _root()
    if not root.is_dir():
        return []
    found = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not DAY.fullmatch(entry.name):
            continue
        folder = _day_dir(entry.name)
        if folder is None or not folder.is_dir():
            continue
        try:
            clips = list(folder.iterdir())
        except OSError:
            continue
        if any(CLIP.fullmatch(clip.name) for clip in clips):
            found.append(entry.name)
    found.sort(reverse=True)
    return found


def list_recordings(day=""):
    today = time.strftime("%Y-%m-%d")
    chosen = day or today
    if not DAY.fullmatch(chosen):
        raise ValueError("tanggal tidak valid")
    folder = _day_dir(chosen)
    grouped = {}
    if folder is not None and folder.is_dir():
        try:
            clips = list(folder.iterdir())
        except OSError:
            clips = []
        now = time.time()
        for clip in clips:
            if not CLIP.fullmatch(clip.name):
                continue
            try:
                stats = clip.stat()
            except OSError:
                continue
            hour = clip.name[:2]
            bucket = grouped.setdefault(hour, {"hour": hour, "size": 0, "recording": False, "files": []})
            bucket["size"] += stats.st_size
            if now - stats.st_mtime < 180:
                bucket["recording"] = True
            bucket["files"].append(
                {
                    "name": clip.name,
                    "start": clip.name[:8].replace("-", ":"),
                    "size": stats.st_size,
                }
            )
    rows = []
    for hour in sorted(grouped, reverse=True):
        bucket = grouped[hour]
        bucket["files"].sort(key=lambda item: item["name"])
        rows.append(bucket)
    return {"today": today, "date": chosen, "dates": _dates(), "rows": rows}


def clip_path(day, name):
    if not CLIP.fullmatch(name or ""):
        return None
    folder = _day_dir(day)
    if folder is None:
        return None
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        return None
    return path
