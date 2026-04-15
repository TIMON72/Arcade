import asyncio
import os
import sys
import signal
import socket
from aiohttp import web



path = os.path.dirname(os.path.abspath(__file__))
path_project = os.path.dirname(path)
sys.path.append(path_project)
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
    queue_main.put(response)
    print(f"SERVER get action={response}")
    return web.json_response({'action': response})


# Настройка сервера
async def server_setup(queue_main):
    app = web.Application(client_max_size=1024**8)
    app['queue_main'] = queue_main  # Сохраняем очередь в app
    app.add_routes(routes)
    app.add_routes(
        [
            web.static("/", path),
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
        print(f"WEB_SERVER IS STARTED on {host}:{port}")
        
        # Ждём бесконечно (пока не придёт сигнал завершения)
        while True:
            await asyncio.sleep(3600)
            
    except OSError as e:
        if e.errno == 98 and retry_count < max_retries:  # Address already in use
            retry_count += 1
            print(f"ERROR: Port {port} is already in use (attempt {retry_count}/{max_retries})")
            print("Killing any lingering processes on this port...")
            os.system(f"fuser -k {port}/tcp 2>/dev/null || true")
            print(f"Retrying in 5 seconds...")
            await asyncio.sleep(5)
            # Рекурсивный вызов для retry
            await server_start(queue_main, host, port, retry_count, max_retries)
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


# Запуск сервера (синхронно -> асинхронно)
def server_start_async(queue_main=None, host="0.0.0.0", port=5000):
    """
    Запускает веб-сервер с поддержкой graceful shutdown
    """
    def signal_handler(signum, frame):
        """Обработчик сигналов для graceful shutdown"""
        print(f"\nReceived signal {signum}, shutting down gracefully...")
        # Отправляем сигнал завершения в event loop
        for task in asyncio.all_tasks():
            task.cancel()
    
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        # Создаём new event loop для безопасности в multiprocessing
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Запускаем сервер
        loop.run_until_complete(server_start(queue_main, host, port))
        
    except KeyboardInterrupt:
        print("\nWEB_SERVER IS STOPPED (KeyboardInterrupt)")
    except asyncio.CancelledError:
        print("\nWEB_SERVER IS STOPPED (Cancelled)")
    except Exception as ex:
        print(f"WEB_SERVER ERROR: {ex}")
    finally:
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