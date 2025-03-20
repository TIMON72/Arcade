import asyncio
import os
import sys
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
async def server_start(queue_main, host, port):
    try:
        # Настройка сервера
        app = await server_setup(queue_main)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, host, port)
        # Запуск сервера
        await server.start()
        print(f"WEB_SERVER IS STARTED on {host}:{port}")
        while True:
            await asyncio.sleep(3600)
    except Exception as ex:
        raise ex
    finally:
        await runner.cleanup()


# Запуск сервера (синхронно -> асинхронно)
def server_start_async(queue_main = None, host="0.0.0.0", port=5000):
    asyncio.run(server_start(queue_main, host, port))


if __name__ == "__main__":
    try:
        server_start_async()
    except KeyboardInterrupt:
        print("\nWEB_SERVER IS STOPPED")
    except Exception as ex:
        print(ex)