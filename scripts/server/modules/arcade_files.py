"""Безопасные операции с ROM Batocera."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_UPLOAD_BYTES = 2 << 30
TRASH_RETENTION_SECONDS = 7 * 24 * 60 * 60


def _filename(value: str) -> str:
    name = value.strip()
    if not name or name in {".", ".."} or len(name) > 180:
        raise ValueError("invalid filename")
    if name.startswith(".") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("invalid filename")
    if any(ord(char) < 32 for char in name):
        raise ValueError("invalid filename")
    return name


def _system(value: str) -> str:
    name = _filename(value)
    if not all(char.isalnum() or char in "_+-" for char in name):
        raise ValueError("invalid system")
    return name


def _game_path(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]
    return _filename(raw)


def _atomic_replace(source: Path, target: Path) -> None:
    if hasattr(os, "sync"):
        os.sync()
    os.replace(source, target)


def _write_gamelist(path: Path, root: ET.Element) -> None:
    temporary = path.with_suffix(".xml.files-new")
    ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
    _atomic_replace(temporary, path)


def _quarantine(source: Path, kind: str, system: str = "") -> str:
    trash = Path(os.environ.get("ARCADE_TRASH_ROOT", "/userdata/system/arcade-trash")) / kind
    trash.mkdir(parents=True, exist_ok=True)
    prefix = f"{system}_" if system else ""
    target = trash / f"{int(time.time())}_{prefix}{source.name}"
    suffix = 1
    while target.exists():
        target = trash / f"{int(time.time())}_{prefix}{suffix}_{source.name}"
        suffix += 1
    os.replace(source, target)
    return str(target)


def delete_game(roms_root: str, system: str, game_path: str) -> dict:
    system = _system(system)
    game_path = _game_path(game_path)
    system_dir = Path(roms_root) / system
    gamelist = system_dir / "gamelist.xml"
    root = ET.parse(gamelist).getroot()
    entry = None
    for candidate in root.findall("game"):
        if (candidate.findtext("path") or "").strip().replace("\\", "/").removeprefix("./") == game_path:
            entry = candidate
            break
    if entry is None:
        raise FileNotFoundError("game not found in gamelist")
    rom = system_dir / game_path
    if not rom.is_file():
        raise FileNotFoundError("ROM file not found")
    backup = _quarantine(rom, "games", system)
    try:
        root.remove(entry)
        _write_gamelist(gamelist, root)
    except Exception:
        os.replace(backup, rom)
        raise
    return {"system": system, "path": game_path, "name": entry.findtext("name") or rom.stem, "backup": backup}


def cleanup_trash(now: float | None = None) -> dict:
    root = Path(os.environ.get("ARCADE_TRASH_ROOT", "/userdata/system/arcade-trash"))
    if not root.is_dir():
        return {"files": 0, "bytes": 0}
    cutoff = (now or time.time()) - TRASH_RETENTION_SECONDS
    removed = 0
    removed_bytes = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        if stat.st_mtime >= cutoff:
            continue
        removed += 1
        removed_bytes += stat.st_size
        path.unlink()
    return {"files": removed, "bytes": removed_bytes}


class UploadManager:
    def __init__(self, roms_root: str, work_root: str = "/userdata/system/.uploads"):
        self.roms_root = Path(roms_root)
        self.work = Path(work_root)
        self.work.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, dict] = {}

    def begin(self, params: dict) -> dict:
        if params.get("kind") != "game":
            raise ValueError("kind must be game")
        filename = _filename(str(params.get("filename", "")))
        system = _system(str(params.get("system", "")))
        size = int(params.get("size", 0))
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise ValueError("invalid upload size")
        if (self.roms_root / system / filename).exists():
            raise FileExistsError("file already exists")
        upload_id = uuid.uuid4().hex
        part = self.work / f"{upload_id}.part"
        part.touch()
        self.items[upload_id] = {
            "kind": "game",
            "system": system,
            "filename": filename,
            "size": size,
            "part": part,
            "offset": 0,
            "sha256": hashlib.sha256(),
        }
        return {"uploadId": upload_id, "chunkSize": 4 * 1024 * 1024}

    def chunk(self, params: dict) -> dict:
        upload_id = str(params.get("uploadId", ""))
        item = self.items.get(upload_id)
        if item is None:
            raise ValueError("unknown uploadId")
        offset = int(params.get("offset", -1))
        if offset != item["offset"]:
            raise ValueError("unexpected chunk offset")
        raw = base64.b64decode(str(params.get("data", "")), validate=True)
        if not raw or len(raw) > 4 * 1024 * 1024 or item["offset"] + len(raw) > item["size"]:
            raise ValueError("invalid chunk")
        with item["part"].open("ab") as output:
            output.write(raw)
        item["offset"] += len(raw)
        item["sha256"].update(raw)
        return {"received": item["offset"], "size": item["size"]}

    def end(self, params: dict) -> dict:
        upload_id = str(params.get("uploadId", ""))
        item = self.items.get(upload_id)
        if item is None:
            raise ValueError("unknown uploadId")
        if item["offset"] != item["size"]:
            raise ValueError("upload is incomplete")
        expected = str(params.get("sha256", "")).lower()
        actual = item["sha256"].hexdigest()
        if expected and actual != expected:
            raise ValueError("sha256 mismatch")
        part = item["part"]
        system_dir = self.roms_root / item["system"]
        gamelist = system_dir / "gamelist.xml"
        target = system_dir / item["filename"]
        root = ET.parse(gamelist).getroot()
        entry = ET.Element("game")
        ET.SubElement(entry, "path").text = f"./{item['filename']}"
        ET.SubElement(entry, "name").text = target.stem
        ET.SubElement(entry, "playcount").text = "0"
        root.append(entry)
        _atomic_replace(part, target)
        try:
            _write_gamelist(gamelist, root)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        target.chmod(0o666)
        self.items.pop(upload_id, None)
        return {"kind": item["kind"], "system": item["system"], "filename": item["filename"], "sha256": actual}

    def abort(self, params: dict) -> dict:
        upload_id = str(params.get("uploadId", ""))
        item = self.items.pop(upload_id, None)
        if item is not None:
            item["part"].unlink(missing_ok=True)
        return {"aborted": True}
