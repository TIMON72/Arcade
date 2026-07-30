import asyncio
import os
import queue
import signal
import sys

from aiohttp import web

import session

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)
routes = web.RouteTableDef()


def _drain(q) -> None:
    if q is None:
        return
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


@routes.get("/")
async def index(request: web.Request):
    rem = int(session.remaining_sec())
    html = f"""
        <!DOCTYPE html>
        <html>
            <head>
                <title>Сервер запущен</title>
            </head>
            <body>
                <h1>Сервер запущен</h1>
                <p>Сессия: {rem}с</p>
            </body>
        </html>
    """
    return web.Response(text=html, content_type="text/html")


@routes.get("/test")
async def test(request: web.Request):
    response = request.query.get("action")
    queue_main = request.app["queue_main"]
    if queue_main is not None and response:
        queue_main.put(response)
    print(f"SERVER get action={response}")
    return web.json_response({"action": response})


@routes.get("/paywall/status")
async def paywall_status(request: web.Request):
    rem = session.remaining_sec()
    return web.json_response(
        {
            "remaining_sec": int(rem),
            "active": rem > 0,
            "in_game": bool(request.app.get("in_game")),
        }
    )


@routes.get("/paywall/wait")
async def paywall_wait(request: web.Request):
    """Block until paywall finishes (gameStart). Skip UI if session still active."""
    queue_to_ui = request.app.get("queue_to_ui")
    queue_from_ui = request.app.get("queue_from_ui")
    if queue_to_ui is None or queue_from_ui is None:
        return web.json_response(
            {"status": "error", "message": "paywall queues missing"}, status=500
        )

    loop = asyncio.get_running_loop()
    lock = request.app["paywall_lock"]
    async with lock:
        shared = request.app.get("paywall_shared")
        if shared is not None and not shared.done():
            is_leader = False
        else:
            shared = loop.create_future()
            request.app["paywall_shared"] = shared
            is_leader = True

    if not is_leader:
        print("SERVER paywall/wait → join in-flight wait")
        result = await shared
        return web.json_response(result)

    try:
        rem = session.remaining_sec()
        if rem > 0:
            result = {
                "status": "ok",
                "message": "session_active",
                "remaining_sec": int(rem),
            }
            print(f"SERVER paywall/wait → session skip ({int(rem)}s left)")
            request.app["in_game"] = True
            if not shared.done():
                shared.set_result(result)
            return web.json_response(result)

        _drain(queue_from_ui)
        _drain(queue_to_ui)

        meta = {
            "cmd": "show",
            "reason": "launch",
            "system": request.query.get("system", ""),
            "emulator": request.query.get("emulator", ""),
            "rom": request.query.get("rom", ""),
        }
        queue_to_ui.put(meta)
        print(f"SERVER paywall/wait → UI show {meta}")

        def wait_result():
            try:
                return queue_from_ui.get(timeout=600)
            except queue.Empty:
                return {"status": "cancelled", "message": "timeout"}

        result = await loop.run_in_executor(None, wait_result)
        if not isinstance(result, dict):
            result = {"status": "cancelled", "message": "bad_result"}

        if result.get("status") == "ok":
            request.app["in_game"] = True
        else:
            request.app["in_game"] = False

        print(f"SERVER paywall/wait ← {result}")
        if not shared.done():
            shared.set_result(result)
        return web.json_response(result)
    except Exception as exc:
        fail = {"status": "cancelled", "message": f"wait_error:{exc}"}
        if not shared.done():
            shared.set_result(fail)
        raise
    finally:
        async with lock:
            if request.app.get("paywall_shared") is shared:
                request.app["paywall_shared"] = None


@routes.get("/paywall/cancel")
async def paywall_cancel(request: web.Request):
    queue_to_ui = request.app.get("queue_to_ui")
    queue_from_ui = request.app.get("queue_from_ui")
    if queue_to_ui is not None:
        queue_to_ui.put({"cmd": "cancel"})
    if queue_from_ui is not None:
        try:
            queue_from_ui.put_nowait({"status": "cancelled", "message": "cancel_api"})
        except Exception:
            pass
    return web.json_response({"status": "ok"})


@routes.get("/paywall/game-stop")
async def paywall_game_stop(request: web.Request):
    """gameStop — end current launch, keep paid session for next game."""
    request.app["in_game"] = False
    queue_to_ui = request.app.get("queue_to_ui")
    if queue_to_ui is not None:
        queue_to_ui.put({"cmd": "cancel"})
    rem = int(session.remaining_sec())
    print(f"SERVER game-stop (session {rem}s left)")
    return web.json_response({"status": "ok", "remaining_sec": rem})


async def server_setup(queue_main, queue_to_ui=None, queue_from_ui=None):
    app = web.Application(client_max_size=1024**8)
    app["queue_main"] = queue_main
    app["queue_to_ui"] = queue_to_ui
    app["queue_from_ui"] = queue_from_ui
    app["paywall_shared"] = None
    app["paywall_lock"] = asyncio.Lock()
    app["in_game"] = False
    app.add_routes(routes)
    app.add_routes([web.static("/static", SCRIPTS_DIR)])
    return app


async def server_start(
    queue_main,
    host,
    port,
    retry_count=0,
    max_retries=3,
    queue_to_ui=None,
    queue_from_ui=None,
):
    runner = None
    try:
        app = await server_setup(queue_main, queue_to_ui, queue_from_ui)
        runner = web.AppRunner(app)
        await runner.setup()

        server = web.TCPSite(runner, host, port, reuse_port=True)
        await server.start()
        print(f"WEB_SERVER IS STARTED on {host}:{port}")

        while True:
            await asyncio.sleep(3600)

    except OSError as e:
        if e.errno == 98 and retry_count < max_retries:
            retry_count += 1
            print(f"ERROR: Port {port} is already in use (attempt {retry_count}/{max_retries})")
            print("Killing any lingering processes on this port...")
            os.system(f"fuser -k {port}/tcp 2>/dev/null || true")
            print("Retrying in 5 seconds...")
            await asyncio.sleep(5)
            await server_start(
                queue_main,
                host,
                port,
                retry_count,
                max_retries,
                queue_to_ui,
                queue_from_ui,
            )
        else:
            print(f"ERROR: Cannot bind to port {port}: {e}")
            raise e
    except Exception as ex:
        print(f"ERROR in server: {ex}")
        raise ex
    finally:
        if runner is not None:
            print("Cleaning up server resources...")
            await runner.cleanup()
            print("Server cleanup completed")


def server_start_async(
    queue_main=None,
    host="0.0.0.0",
    port=5000,
    queue_to_ui=None,
    queue_from_ui=None,
):
    def signal_handler(signum, frame):
        print(f"\nReceived signal {signum}, shutting down gracefully...")
        for task in asyncio.all_tasks():
            task.cancel()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            server_start(
                queue_main,
                host,
                port,
                queue_to_ui=queue_to_ui,
                queue_from_ui=queue_from_ui,
            )
        )
    except KeyboardInterrupt:
        print("\nWEB_SERVER IS STOPPED (KeyboardInterrupt)")
    except asyncio.CancelledError:
        print("\nWEB_SERVER IS STOPPED (Cancelled)")
    except Exception as ex:
        print(f"WEB_SERVER ERROR: {ex}")
    finally:
        if loop is not None:
            print("Closing event loop...")
            loop.close()
            print("WEB_SERVER: All resources released")


if __name__ == "__main__":
    try:
        server_start_async()
    except KeyboardInterrupt:
        print("\nWEB_SERVER IS STOPPED")
    except Exception as ex:
        print(ex)
