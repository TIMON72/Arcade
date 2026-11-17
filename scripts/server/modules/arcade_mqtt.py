"""MQTT online и каталог игр Batocera для Вендоматики."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from main import log
from modules import arcade_files

CONFIG_PATH = "/userdata/system/mqtt.json"
ROMS_PATH = "/userdata/roms"
MAX_GAMES_PAYLOAD_BYTES = 4 << 20


def parse_last_played(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), timezone.utc).isoformat().replace("+00:00", "Z")
        if "T" in raw and len(raw) == 15 and raw[:8].isdigit():
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            parsed = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=local_tz)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return ""


def _restore_empty_gamelist(gamelist: Path, datainit_root: Path) -> None:
    if gamelist.stat().st_size != 0:
        return
    source = datainit_root / gamelist.parent.name / "gamelist.xml"
    if not source.is_file() or source.stat().st_size == 0:
        return
    temporary = gamelist.with_suffix(".xml.mqtt-new")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, gamelist)
        if hasattr(os, "sync"):
            os.sync()
        log(f"MQTT games: restored empty {gamelist} from Batocera datainit")
    finally:
        if temporary.exists():
            temporary.unlink()


def load_games(
    roms_root: str = ROMS_PATH,
    datainit_root: str = "/usr/share/batocera/datainit/roms",
    systems: list[str] | None = None,
) -> list[dict]:
    root = Path(roms_root)
    defaults = Path(datainit_root)
    allowed = set(systems or [])
    games: dict[tuple[str, str], dict] = {}
    for gamelist in sorted(root.glob("*/gamelist.xml")):
        system = gamelist.parent.name
        if allowed and system not in allowed:
            continue
        try:
            _restore_empty_gamelist(gamelist, defaults)
        except OSError as error:
            log(f"MQTT games: cannot restore {gamelist}: {error}")
        try:
            tree = ET.parse(gamelist)
        except (ET.ParseError, OSError) as error:
            log(f"MQTT games: cannot read {gamelist}: {error}")
            continue
        for node in tree.getroot().findall("game"):
            game_path = (node.findtext("path") or "").strip()
            if not game_path:
                continue
            rom = gamelist.parent / game_path.removeprefix("./")
            if not rom.is_file():
                continue
            name = (node.findtext("name") or "").strip() or Path(game_path).stem
            try:
                play_count = max(0, int((node.findtext("playcount") or "0").strip()))
            except ValueError:
                play_count = 0
            try:
                game_time = max(0, int((node.findtext("gametime") or "0").strip()))
            except ValueError:
                game_time = 0
            games[(system, game_path)] = {
                "system": system,
                "path": game_path,
                "name": name,
                "playCount": play_count,
                "gameTimeSeconds": game_time,
                "lastPlayed": parse_last_played(node.findtext("lastplayed") or ""),
                "favorite": (node.findtext("favorite") or "").strip().lower() == "true",
            }
    return sorted(games.values(), key=lambda game: (-game["playCount"], game["name"].lower()))


def load_config(path: str = CONFIG_PATH) -> dict | None:
    try:
        with open(path, encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        log(f"MQTT: cannot read {path}: {error}")
        return None
    if not isinstance(config, dict) or not config.get("enabled"):
        return None
    machine_id = str(config.get("machineId", "")).strip()
    if not machine_id:
        log("MQTT: machineId is required")
        return None
    try:
        broker = urlparse(str(config.get("broker", "")).strip())
        if broker.scheme != "tcp" or not broker.hostname:
            raise ValueError("broker must use tcp://host:port")
        config["_host"] = broker.hostname
        config["_port"] = broker.port or 1883
    except ValueError as error:
        log(f"MQTT: {error}")
        return None
    config["machineId"] = machine_id
    config["topicPrefix"] = str(config.get("topicPrefix") or f"tcn/{machine_id}").strip("/")
    return config


def refresh_games_ui() -> None:
    try:
        result = subprocess.run(
            ["batocera-es-swissknife", "--restart"],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            log(f"MQTT games: EmulationStation restart failed rc={result.returncode}")
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"MQTT games: EmulationStation restart failed: {error}")


class ArcadeMqtt:
    def __init__(self, config: dict):
        self.config = config
        self.client = None
        self.connected = False
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_heartbeat = 0.0
        self.last_games_scan = 0.0
        self.games_fingerprint = ""
        self.games_count = 0
        self.uploads = arcade_files.UploadManager(ROMS_PATH)

    @property
    def prefix(self) -> str:
        return self.config["topicPrefix"]

    def start(self) -> bool:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log("MQTT: paho-mqtt is not installed")
            return False

        status_topic = f"{self.prefix}/status"
        offline = json.dumps({
            "state": "offline",
            "machineId": self.config["machineId"],
            "source": "raspberry",
            "ts": int(time.time()),
        })
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id=f"arcade-{self.config['machineId']}-{os.getpid()}",
        )
        if self.config.get("username"):
            self.client.username_pw_set(
                str(self.config["username"]),
                str(self.config.get("password", "")),
            )
        self.client.will_set(status_topic, offline, qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.connect_async(self.config["_host"], self.config["_port"], keepalive=30)
        self.client.loop_start()
        self.worker = threading.Thread(target=self._run, name="arcade-mqtt", daemon=True)
        self.worker.start()
        log(f"MQTT: connecting to {self.config['_host']}:{self.config['_port']} as {self.config['machineId']}")
        return True

    def _on_connect(self, client, _userdata, _flags, rc):
        if rc != 0:
            log(f"MQTT: connection refused rc={rc}")
            return
        self.connected = True
        client.subscribe(f"{self.prefix}/cmd", qos=1)
        log("MQTT: connected")
        self.publish_status()
        self.publish_games(force=True)

    def _on_message(self, _client, _userdata, message) -> None:
        request_id = ""
        try:
            body = json.loads(message.payload)
            request_id = str(body.get("id", ""))
            action = str(body.get("action", ""))
            params = body.get("params") or {}
            if action == "ping":
                data = {"pong": True, "features": ["games", "upload", "delete"]}
            elif action == "delete_game":
                data = arcade_files.delete_game(ROMS_PATH, str(params.get("system", "")), str(params.get("path", "")))
                self.publish_games(force=True)
                refresh_games_ui()
            elif action == "upload_begin":
                data = self.uploads.begin(params)
            elif action == "upload_chunk":
                data = self.uploads.chunk(params)
            elif action == "upload_end":
                data = self.uploads.end(params)
                if data.get("kind") == "game":
                    self.publish_games(force=True)
                    refresh_games_ui()
            elif action == "upload_abort":
                data = self.uploads.abort(params)
            else:
                raise ValueError("unknown action")
            self._publish("response", {"id": request_id, "ok": True, "data": data})
        except Exception as error:
            log(f"MQTT command error: {error}")
            self._publish("response", {"id": request_id, "ok": False, "error": str(error)})

    def _on_disconnect(self, _client, _userdata, _rc):
        self.connected = False
        if not self.stop_event.is_set():
            log("MQTT: disconnected")

    def _publish(self, kind: str, payload: dict, retain: bool = False) -> None:
        if self.client is None or not self.connected:
            return
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.client.publish(f"{self.prefix}/{kind}", raw, qos=1, retain=retain)

    def publish_status(self) -> None:
        self.last_heartbeat = time.monotonic()
        self._publish("status", {
            "state": "online",
            "machineId": self.config["machineId"],
            "source": "raspberry",
            "heartbeat": True,
            "gamesCount": self.games_count,
            "ts": int(time.time()),
        }, retain=True)

    def publish_games(self, force: bool = False) -> None:
        self.last_games_scan = time.monotonic()
        games = load_games(systems=self.config.get("gameSystems"))
        fingerprint = hashlib.sha256(
            json.dumps(games, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if not force and fingerprint == self.games_fingerprint:
            return
        payload = {
            "type": "arcade.games",
            "machineId": self.config["machineId"],
            "ts": int(time.time()),
            "games": games,
        }
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if size > MAX_GAMES_PAYLOAD_BYTES:
            log(f"MQTT games: payload is too large ({size} bytes)")
            return
        self._publish("event", payload)
        self.games_fingerprint = fingerprint
        self.games_count = len(games)
        log(f"MQTT games: published {len(games)} game(s)")

    def _run(self) -> None:
        heartbeat_sec = max(10, int(self.config.get("heartbeatSec", 30)))
        games_sec = max(30, int(self.config.get("gamesRefreshSec", 60)))
        last_cleanup = 0.0
        while not self.stop_event.wait(1):
            now = time.monotonic()
            if now - last_cleanup >= 24 * 60 * 60:
                last_cleanup = now
                result = arcade_files.cleanup_trash()
                if result["files"]:
                    log(f"MQTT trash: removed {result['files']} old file(s), {result['bytes']} bytes")
            if not self.connected:
                continue
            if now - self.last_heartbeat >= heartbeat_sec:
                self.publish_status()
            if now - self.last_games_scan >= games_sec:
                self.publish_games()

    def stop(self) -> None:
        self.stop_event.set()
        if self.client is not None and self.connected:
            self._publish("status", {
                "state": "offline",
                "machineId": self.config["machineId"],
                "source": "raspberry",
                "ts": int(time.time()),
            }, retain=True)
            time.sleep(0.2)
        if self.client is not None:
            self.client.disconnect()
            self.client.loop_stop()
        if self.worker is not None:
            self.worker.join(timeout=2)


def start() -> ArcadeMqtt | None:
    config = load_config()
    if config is None:
        return None
    agent = ArcadeMqtt(config)
    if not agent.start():
        return None
    return agent
