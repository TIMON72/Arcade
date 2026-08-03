import asyncio
import os
import sys
import signal
from aiohttp import web


SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)
import main as app_main

routes = web.RouteTableDef()


# Главная страница
@routes.get('/')
async def index(request: web.Request):
    html = f"""
        <!DOCTYPE html>
        <html>
            <head>
                <title>Сервер запущен</title>
            </head>
            <body>
                <h1>Сервер запущен</h1>
            </body>
        </html>
    """
    return web.Response(text=html, content_type="text/html")


# Страница /test
@routes.get('/test')
async def test(request: web.Request):
    response = request.query.get('action')
    queue_main = request.app['queue_main']
    if response:
        queue_main.put(response)
    return web.json_response({'action': response})


# Настройка сервера
async def server_setup(queue_main):
    app = web.Application(client_max_size=1024**8)
    app['queue_main'] = queue_main  # Сохраняем очередь в app
    app.add_routes(routes)
    app.add_routes(
        [
            web.static("/", SCRIPTS_DIR),
        ]
    )
    return app


# Запуск сервера
async def server_start(queue_main, host, port, retry_count=0, max_retries=3):
    runner = None
    try:
        # Настройка сервера
        app = await server_setup(queue_main)
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Создаём TCPSite с переиспользованием адреса
        server = web.TCPSite(runner, host, port, reuse_port=True)
        
        # Запуск сервера
        await server.start()
        app_main.log(f"WEB_SERVER started on {host}:{port}")
        
        # Ждём бесконечно (пока не придёт сигнал завершения)
        while True:
            await asyncio.sleep(3600)
            
    except OSError as e:
        if e.errno == 98 and retry_count < max_retries:  # Address already in use
            retry_count += 1
            app_main.log(f"ERROR: Port {port} is already in use (attempt {retry_count}/{max_retries})")
            app_main.log("Killing any lingering processes on this port...")
            os.system(f"fuser -k {port}/tcp 2>/dev/null || true")
            app_main.log("Retrying in 5 seconds...")
            await asyncio.sleep(5)
            # Рекурсивный вызов для retry
            await server_start(queue_main, host, port, retry_count, max_retries)
        else:
            app_main.log(f"ERROR: Cannot bind to port {port}: {e}")
            raise e
    except Exception as ex:
        app_main.log(f"ERROR in server: {ex}")
        raise ex
    finally:
        if runner is not None:
            app_main.log("Cleaning up server resources...")
            await runner.cleanup()
            app_main.log("Server cleanup completed")


# Запуск сервера (синхронно -> асинхронно)
def server_start_async(queue_main=None, host="0.0.0.0", port=5000):
    """
    Запускает веб-сервер с поддержкой graceful shutdown
    """
    def signal_handler(signum, frame):
        """Обработчик сигналов для graceful shutdown"""
        app_main.log(f"Received signal {signum}, shutting down gracefully...")
        for task in asyncio.all_tasks():
            task.cancel()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server_start(queue_main, host, port))

    except KeyboardInterrupt:
        app_main.log("WEB_SERVER stopped (KeyboardInterrupt)")
    except asyncio.CancelledError:
        app_main.log("WEB_SERVER stopped (Cancelled)")
    except Exception as ex:
        app_main.log(f"WEB_SERVER ERROR: {ex}")
    finally:
        if loop is not None:
            app_main.log("Closing event loop...")
            loop.close()
            app_main.log("WEB_SERVER: All resources released")


if __name__ == "__main__":
    try:
        server_start_async()
    except KeyboardInterrupt:
        app_main.log("WEB_SERVER stopped")
    except Exception as ex:
        app_main.log(str(ex))
