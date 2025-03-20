import multiprocessing
import time
import server
import timer

def main():
    queue_main = multiprocessing.Queue()
    # Запускаем server.py в отдельном процессе
    server_process = multiprocessing.Process(
        target=server.server_start_async, 
        args=(queue_main,)
    )
    server_process.start()
    # Запускаем timer.py в отдельном процессе
    timer_process = multiprocessing.Process(
        target=timer.loop, 
        args=(queue_main,)
    )
    timer_process.start()
    # Основной цикл main.py
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("MAIN service IS STOPPED")
    finally:
        server_process.terminate()
        timer_process.terminate()
        server_process.join()
        timer_process.join()

if __name__ == "__main__":
    main()