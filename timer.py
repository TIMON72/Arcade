import queue
import lgpio
import time


# Класс кнопки
class Button:
    def __init__(self, h, pin):
        self.h = h
        self.pin = pin
        self.prevState = False
    def isClicked(self):
        time.sleep(0.03)
        curState = lgpio.gpio_read(self.h, self.pin)
        # Нажата
        if curState and not self.prevState:
            self.prevState = curState
            return False
        # Отпущена
        elif not curState and self.prevState:
            self.prevState = curState
            return True
        return False


# Константы (PIN)
RF_INCREASE = 5 # Кнопка "+"
RF_PLAYPAUSE = 6 # Кнопка "Play/Pause"
RF_STOP = 13 # Кнопка "Stop"
R_BUTTONS = 17 # Реле IN1-IN6
R_PLAYPAUSE = 27 # Реле IN7
R_STOP = 22 # Реле IN8
# Подключаемся к чипу GPIO
h = lgpio.gpiochip_open(4)  # Открываем gpiochip0.  Проверьте, что имя чипа верное (ls /dev/gpiochip*)
# Привязка кнопок к PIN
b_increase = Button(h, RF_INCREASE)
b_playpause = Button(h, RF_PLAYPAUSE)
b_stop = Button(h, RF_STOP)
# Режим активации реле
isRelayLow = True # Флаг: тип активации реле = low level
isRelayHigh = not isRelayLow # Флаг: тип активации реле = high level
# Состояния автомата (через состояния реле)
state_starting = True
state_playing = False
state_waiting = False


# Конфигурация Raspberry
def setup():
    # Настраиваем PIN на выходной сигнал
    lgpio.gpio_claim_output(h, RF_INCREASE)
    lgpio.gpio_claim_output(h, RF_PLAYPAUSE)
    lgpio.gpio_claim_output(h, RF_STOP)
    # Настраиваем PIN на входной сигнал
    lgpio.gpio_claim_input(h, R_BUTTONS)
    lgpio.gpio_claim_input(h, R_PLAYPAUSE)
    lgpio.gpio_claim_input(h, R_STOP)


# Главная цикличная функция Raspberry
def loop(queue_main = None):
    while True:
        updateState()
    # if b_increase.isClicked():
    #     action(RF_INCREASE)
    # elif b_playpause.isClicked():
    #     action(RF_PLAYPAUSE)
    # elif b_stop.isClicked():
    #     action(RF_STOP)
        try:
            signal = queue_main.get(timeout=0.1)
            action(signal)
        except queue.Empty:
            pass
        time.sleep(0.1)


# Тестирование
def test():
    rf_click(RF_INCREASE)
    print("RF_INCREASE clicked")
    time.sleep(10)


# Действие по заданному сигналу
def action(signal):
    if type(signal) == int:
        if signal == RF_INCREASE:
            rf_click(RF_INCREASE)
        elif signal == RF_PLAYPAUSE:
            rf_click(RF_PLAYPAUSE)
        elif signal == RF_STOP:
            rf_click(RF_STOP)
    elif type(signal) == str:
        global state_starting
        global state_playing
        global state_waiting
        # Стандартные команды
        if signal == 'INCREASE':
            rf_click(RF_INCREASE)
        elif signal == 'PLAYPAUSE':
            rf_click(RF_PLAYPAUSE)
        elif signal == 'STOP':
            rf_click(RF_STOP)
        # Многосоставные команды
        elif 'ADD' in signal:
            increase_count = int(int(signal.split('_')[1]) / 5)
            def increase_clicks(increase_reduce: int = 0):
                for i in range(increase_count - increase_reduce):
                    rf_click(RF_INCREASE)
                    time.sleep(1)
            if state_starting:
                time.sleep(5)
                increase_clicks()
                rf_click(RF_PLAYPAUSE)
            elif state_playing:
                increase_clicks()
            elif state_waiting:
                rf_click(RF_PLAYPAUSE)
                time.sleep(1)
                increase_clicks(1)
                rf_click(RF_PLAYPAUSE)
            else:
                print("STATE: ERROR")
        


# Имитация нажатия кнопки Радиореле
def rf_click(rf_pin: int):
    lgpio.gpio_write(h, rf_pin, 1)
    time.sleep(1)
    lgpio.gpio_write(h, rf_pin, 0)


# # Активация реле
# def relay_activate(relay: int):
#     if isRelayLow:
#         lgpio.gpio_write(h, relay, 0)
#     elif isRelayHigh:
#         lgpio.gpio_write(h, relay, 1)


# # Деактивация реле
# def relay_deactivate(relay: int):
#     if isRelayLow:
#         lgpio.gpio_write(h, relay, 1)
#     elif isRelayHigh:
#         lgpio.gpio_write(h, relay, 0)


# # Имитация нажатия кнопки реле
# def relay_click(relay: int):
#     relay_activate(relay)
#     time.sleep(1)
#     relay_deactivate(relay)


# Обновление состояние (исходя из сигналов с реле)
def updateState():
    global state_starting
    global state_playing
    global state_waiting
    global isRelayLow
    buttons_state = lgpio.gpio_read(h, R_BUTTONS)
    playpause_state = lgpio.gpio_read(h, R_PLAYPAUSE)
    stop_state = lgpio.gpio_read(h, R_STOP)
    if isRelayLow:
        buttons_state = not buttons_state
        playpause_state = not playpause_state
        stop_state = not stop_state
    # Нажаты все кнопки? : Реле не подключено
    if buttons_state and playpause_state and stop_state:
        print("STATE: ERROR")
        state_starting = False
        state_playing = False
        state_waiting = False
    # Нажат STOP? : сброс
    elif not state_starting and stop_state:
        print("STATE: STARTING")
        state_starting = True
        state_playing = False
        state_waiting = False
    # Кнопки неактивны, но был нажат PLAYPAUSE? : игра
    elif not state_playing and not buttons_state and playpause_state:
        print("STATE: PLAYING")
        state_starting = False
        state_playing = True
        state_waiting = False
    # Кнопки активны и был нажат PLAYPAUSE? : ожидание
    elif not state_waiting and buttons_state and playpause_state:
        print("STATE: WAITING")
        state_starting = False
        state_playing = False
        state_waiting = True


# Главная функция
if __name__ == "__main__":
    try:
        # asyncio.run(main())
        setup()
        loop()
    except KeyboardInterrupt:
        pass
    finally:
        lgpio.gpiochip_close(h)
