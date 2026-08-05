#!/usr/bin/python3
"""
HTTP-сервис вебхуков (отдельный процесс Batocera).

Сценарии разводятся через scripts/server/modules/…
Сейчас: /test?action=… → modules.timer.send() → Unix-сокет → сервис timer.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tomllib
from dataclasses import dataclass
from typing import Optional

from aiohttp import web

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_SERVER_DIR)
_MAIN_DIR = os.path.join(_SCRIPTS_ROOT, "main")
_CONFIG_PATH = os.path.join(_SERVER_DIR, "config.toml")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
if _MAIN_DIR not in sys.path:
    sys.path.insert(0, _MAIN_DIR)

from main import log
from modules import timer as timer_bridge

routes = web.RouteTableDef()


@dataclass(frozen=True)
class ServerConfig:
    port: int = 5000
    timer_socket: str = "/var/run/arcade-timer.sock"


def load_server_config() -> ServerConfig:
    defaults = ServerConfig()
    if not os.path.isfile(_CONFIG_PATH):
        return defaults
    with open(_CONFIG_PATH, "rb") as config_file:
        data = tomllib.load(config_file)
    section = data.get("server", {})
    if not isinstance(section, dict):
        raise ValueError("[server] must be a table")
    port = section.get("port", defaults.port)
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
        raise ValueError("port must be a positive integer")
    timer_socket = section.get("timer_socket", defaults.timer_socket)
    if not isinstance(timer_socket, str) or not timer_socket.strip():
        raise ValueError("timer_socket must be a non-empty string")
    return ServerConfig(port=port, timer_socket=timer_socket.strip())


server_config = load_server_config()


@routes.get("/")
async def index(_request: web.Request):
    return web.Response(
        text="<html><body><h1>Сервер запущен</h1></body></html>",
        content_type="text/html",
    )


@routes.get("/test")
async def test(request: web.Request):
    """GET /test?action=INCREASE|PLAYPAUSE|STOP|ADD_15 → timer via modules.timer."""
    name = request.query.get("action")
    if not name:
        return web.json_response({"ok": False, "error": "missing action"}, status=400)
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, timer_bridge.send, name)
    if not ok:
        return web.json_response(
            {"action": name, "ok": False, "error": "timer unreachable"},
            status=503,
        )
    log(f"WEB: action={name}")
    return web.json_response({"action": name, "ok": True})


async def _serve(host: str, port: int):
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port, reuse_port=True).start()
        log(f"WEB: listening {host}:{port}")
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


def run(host="0.0.0.0", port: Optional[int] = None):
    if port is None:
        port = server_config.port
    try:
        asyncio.run(_serve(host, port))
    except OSError as e:
        log(f"WEB: bind failed port={port}: {e}")
    except Exception as e:
        log(f"WEB: stopped: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("gameStart", "gameStop"):
        raise SystemExit(0)
    run()
