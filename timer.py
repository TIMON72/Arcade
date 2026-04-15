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

# Группы пинов (для гибкой инициализации/логирования)
OUTPUT_PINS = {
    'RF_INCREASE': RF_INCREASE,
    'RF_PLAYPAUSE': RF_PLAYPAUSE,
    'RF_STOP': RF_STOP,
}
INPUT_PINS = {
    'R_BUTTONS': R_BUTTONS,
    'R_PLAYPAUSE': R_PLAYPAUSE,
    'R_STOP': R_STOP,
}
PIN_TO_OUTPUT_NAME = {pin: name for name, pin in OUTPUT_PINS.items()}

# Глобальные переменные - инициализируются в setup()
h = None
b_increase = None
b_playpause = None
b_stop = None

# Флаги для отслеживания каких контактов удалось выделить
gpio_pins_available = {name: False for name in {**OUTPUT_PINS, **INPUT_PINS}}

# Режим активации реле
isRelayLow = True # Флаг: тип активации реле = low level
isRelayHigh = not isRelayLow # Флаг: тип активации реле = high level
# Состояния автомата (через состояния реле)
state_starting = True
state_playing = False
state_waiting = False

# Однократное предупреждение о неподключенном реле
relay_disconnected_warned = False


def setup():
    global h, b_increase, b_playpause, b_stop, gpio_pins_available
    try:
        gpiochip = None
        for i in range(10):
            try:
                test_h = lgpio.gpiochip_open(i)
                lgpio.gpiochip_close(test_h)
                gpiochip = i
                break
            except Exception:
                continue

        if gpiochip is None:
            raise Exception("No available gpiochip found (tried gpiochip0-9)")

        print(f"Using gpiochip{gpiochip}")
        h = lgpio.gpiochip_open(gpiochip)

        b_increase = Button(h, RF_INCREASE)
        b_playpause = Button(h, RF_PLAYPAUSE)
        b_stop = Button(h, RF_STOP)

        for pin_name, pin in OUTPUT_PINS.items():
            try:
                lgpio.gpio_claim_output(h, pin)
                gpio_pins_available[pin_name] = True
                print(f"GPIO {pin_name}({pin}) -> OK (output)")
            except Exception as e:
                gpio_pins_available[pin_name] = False
                print(f"GPIO {pin_name}({pin}) -> BUSY ({e})")

        for pin_name, pin in INPUT_PINS.items():
            try:
                lgpio.gpio_claim_input(h, pin)
                gpio_pins_available[pin_name] = True
                print(f"GPIO {pin_name}({pin}) -> OK (input)")
            except Exception as e:
                gpio_pins_available[pin_name] = False
                print(f"GPIO {pin_name}({pin}) -> BUSY ({e})")

        print(f"GPIO summary: {sum(gpio_pins_available.values())}/{len(gpio_pins_available)} pins available")
    except Exception as e:
        print(f"ERROR: GPIO initialization failed: {e}")
        h = None
        for key in gpio_pins_available:
            gpio_pins_available[key] = False


# Главная цикличная функция Raspberry
def loop(queue_main = None):
    # Инициализируем GPIO при запуске процесса
    setup()
    
    while True:
        updateState()
    # if b_increase.isClicked():
    #     action(RF_INCREASE)
    # elif b_playpause.isClicked():
    #     action(RF_PLAYPAUSE)
    # elif b_stop.isClicked():
    #     action(RF_STOP)
        try:
            if queue_main is not None:
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
    # Определяем какой флаг проверить
    pin_name = PIN_TO_OUTPUT_NAME.get(rf_pin)
    
    # Если GPIO не инициализирована или контакт недоступен, пропускаем
    if h is None or (pin_name and not gpio_pins_available.get(pin_name, False)):
        print(f"WARNING: Cannot click RF pin {rf_pin} - GPIO not available")
        return
    
    try:
        lgpio.gpio_write(h, rf_pin, 1)
        time.sleep(1)
        lgpio.gpio_write(h, rf_pin, 0)
    except Exception as e:
        print(f"WARNING: Error writing to GPIO pin {rf_pin}: {e}")


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
    global relay_disconnected_warned
    
    # Если GPIO не инициализирована или контакты недоступны, пропускаем
    if h is None or not gpio_pins_available['R_BUTTONS'] or not gpio_pins_available['R_PLAYPAUSE'] or not gpio_pins_available['R_STOP']:
        return
    
    try:
        buttons_state = lgpio.gpio_read(h, R_BUTTONS)
        playpause_state = lgpio.gpio_read(h, R_PLAYPAUSE)
        stop_state = lgpio.gpio_read(h, R_STOP)
    except Exception as e:
        print(f"WARNING: Error reading GPIO: {e}")
        return
    if isRelayLow:
        buttons_state = not buttons_state
        playpause_state = not playpause_state
        stop_state = not stop_state
    # Нажаты все кнопки? : Реле не подключено
    if buttons_state and playpause_state and stop_state:
        if not relay_disconnected_warned:
            print("WARNING: Relay disconnected")
            relay_disconnected_warned = True
        return
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
        print("Timer received KeyboardInterrupt")
    except Exception as e:
        print(f"Timer error: {e}")
    finally:
        # Правильное закрытие GPIO
        if h is not None:
            try:
                print("Closing GPIO chip...")
                lgpio.gpiochip_close(h)
                print("GPIO chip closed successfully")
            except Exception as e:
                print(f"Error closing GPIO chip: {e}")
        print("Timer shutdown complete")
