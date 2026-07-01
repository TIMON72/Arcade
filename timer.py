import queue
import lgpio
import sys
import time
from datetime import datetime

import project_cleanup


# Класс кнопки
class Button:
    def __init__(self, h, pin):
        self.h = h
        self.pin = pin
        self.prevState = False
    def isClicked(self):
        if self.h is None:
            return False
        time.sleep(0.03)
        try:
            curState = lgpio.gpio_read(self.h, self.pin)
        except Exception:
            return False
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
    'R_BUTTONS': R_BUTTONS,
    'R_PLAYPAUSE': R_PLAYPAUSE,
    'R_STOP': R_STOP,
}
INPUT_PINS = {
    'RF_INCREASE': RF_INCREASE,
    'RF_PLAYPAUSE': RF_PLAYPAUSE,
    'RF_STOP': RF_STOP,
}
PIN_TO_OUTPUT_NAME = {pin: name for name, pin in OUTPUT_PINS.items()}
RF_TO_RELAY = {
    RF_PLAYPAUSE: R_PLAYPAUSE,
    RF_STOP: R_STOP,
}

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
# Состояния автомата (для ADD с сервера и логов)
state_starting = True
state_playing = False
state_waiting = False
# Как start / activated / waited в Arduino
start = False
activated = False
waited = False
hours = 0
minutes = 0
seconds = 0
time_main = [0, 0, 0]
time_step = 5
time_max = 24
time_start = 5
time_wait = 60

# Однократное предупреждение о неподключенном реле
relay_disconnected_warned = False
_last_logged_state = None
_timer_line_active = False

RF_PIN_NAMES = {
    RF_INCREASE: 'INCREASE (+)',
    RF_PLAYPAUSE: 'PLAYPAUSE',
    RF_STOP: 'STOP',
}


class TickTimer:
    def __init__(self):
        self._previous = time.monotonic()

    def isTicked(self, ms):
        now = time.monotonic()
        if (now - self._previous) * 1000 >= ms:
            self._previous = now
            return True
        return False

    def refresh(self):
        self._previous = time.monotonic()


tick_timer = TickTimer()


def finish_timer_line():
    global _timer_line_active
    if _timer_line_active:
        sys.stdout.write("\n")
        sys.stdout.flush()
        _timer_line_active = False


def format_timer_display():
    if waited:
        return f"TIMER: WAIT ${seconds:02d}"
    if hours > 0:
        return f"TIMER: {hours:02d}:{minutes:02d}"
    return f"TIMER: {minutes:02d}:{seconds:02d}"


def show_timer_inline():
    global _timer_line_active
    sys.stdout.write(f"\r{format_timer_display()}\033[K")
    sys.stdout.flush()
    _timer_line_active = True


def timer_state_name():
    if state_waiting:
        return "WAITING"
    if state_playing:
        return "PLAYING"
    if state_starting:
        return "START"
    return "UNKNOWN"


def log_timer_state(reason=""):
    global _last_logged_state
    finish_timer_line()
    name = timer_state_name()
    if name == _last_logged_state and not reason:
        return
    _last_logged_state = name
    if reason:
        print(f"STATE: {name} ({reason})")
    else:
        print(f"STATE: {name}")


def log_ready():
    global _last_logged_state
    finish_timer_line()
    _last_logged_state = "READY"
    print("STATE: READY")


def log_button(name, source="radio"):
    finish_timer_line()
    print(f"BTN: {name} ({source})")


def is_timer_empty():
    return hours == 0 and minutes == 0 and seconds == 0


def sync_state_flags():
    global state_starting, state_playing, state_waiting
    if not start:
        state_starting, state_playing, state_waiting = True, False, False
    elif waited:
        state_starting, state_playing, state_waiting = False, False, True
    elif activated:
        state_starting, state_playing, state_waiting = False, True, False
    else:
        state_starting, state_playing, state_waiting = False, False, False


def log_time():
    finish_timer_line()
    print(f"TIME: {hours:02d}:{minutes:02d}:{seconds:02d}")


def print_start_countdown():
    finish_timer_line()
    for counter in range(time_start, 0, -1):
        sys.stdout.write(f"\rCOUNTDOWN: {counter}\033[K")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\n")
    sys.stdout.flush()


def handle_increase(source="radio"):
    global hours, minutes, seconds
    if waited:
        return
    if hours < time_max:
        minutes += time_step
        if minutes > 59:
            hours += 1
            minutes = minutes % 60
    log_time()


def handle_playpause(source="radio"):
    global start, activated, waited, hours, minutes, seconds
    finish_timer_line()
    if not start:
        if is_timer_empty():
            print("BTN: PLAYPAUSE ignored: timer not set (00:00:00)")
            return
        print_start_countdown()
        print(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
        relay_click(R_PLAYPAUSE)
        print(f"RELAY: R_BUTTONS({R_BUTTONS}) activate")
        relay_activate(R_BUTTONS)
        start = True
        activated = True
        waited = False
        sync_state_flags()
        log_timer_state("play")
        tick_timer.refresh()
    elif activated and not waited:
        print(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
        relay_click(R_PLAYPAUSE)
        print(f"RELAY: R_BUTTONS({R_BUTTONS}) deactivate")
        relay_deactivate(R_BUTTONS)
        print("STATE: PAUSE")
        activated = False
        sync_state_flags()
    elif not activated and not waited:
        print_start_countdown()
        print(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
        relay_click(R_PLAYPAUSE)
        print(f"RELAY: R_BUTTONS({R_BUTTONS}) activate")
        relay_activate(R_BUTTONS)
        activated = True
        sync_state_flags()
        log_timer_state("play")
        tick_timer.refresh()
    elif waited and not activated:
        print(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
        relay_click(R_PLAYPAUSE)
        relay_deactivate(R_BUTTONS)
        activated = True
        seconds = time_wait
        sync_state_flags()
        log_timer_state("waiting")
        tick_timer.refresh()
    elif waited and activated:
        start = False
        activated = False
        waited = False
        hours = time_main[0]
        minutes = time_main[1] + time_step
        seconds = time_main[2]
        log_time()
        sync_state_flags()
        log_ready()


def handle_stop(source="radio"):
    global start, activated, waited, hours, minutes, seconds
    finish_timer_line()
    print(f"RELAY: R_STOP({R_STOP}) click")
    relay_click(R_STOP)
    print(f"RELAY: R_BUTTONS({R_BUTTONS}) deactivate")
    relay_deactivate(R_BUTTONS)
    start = False
    activated = False
    waited = False
    hours = time_main[0]
    minutes = time_main[1]
    seconds = time_main[2]
    log_time()
    sync_state_flags()
    log_ready()


def relay_activate(relay: int):
    if isRelayLow:
        lgpio.gpio_write(h, relay, 0)
    elif isRelayHigh:
        lgpio.gpio_write(h, relay, 1)


def relay_deactivate(relay: int):
    if isRelayLow:
        lgpio.gpio_write(h, relay, 1)
    elif isRelayHigh:
        lgpio.gpio_write(h, relay, 0)


def relay_click(relay: int):
    relay_activate(relay)
    time.sleep(1)
    relay_deactivate(relay)


def _on_countdown_finished():
    global activated, waited, seconds
    finish_timer_line()
    if not waited:
        waited = True
        activated = False
        print(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
        relay_click(R_PLAYPAUSE)
        relay_deactivate(R_BUTTONS)
        activated = True
        seconds = time_wait
        sync_state_flags()
        log_timer_state("waiting")
        tick_timer.refresh()
    else:
        activated = False
        handle_stop("timer")


def tick(delay_ms=1000):
    global hours, minutes, seconds, activated, waited, start
    if not start:
        return
    if not tick_timer.isTicked(delay_ms):
        return
    if activated:
        show_timer_inline()
        seconds -= 1
        if seconds < 0:
            seconds = 59
            minutes -= 1
            if minutes < 0:
                minutes = 59
                hours -= 1
                if hours < 0:
                    hours = 0
                    minutes = 0
                    seconds = 0
        if seconds == 0 and minutes == 0 and hours == 0:
            _on_countdown_finished()
    else:
        show_timer_inline()


def gpio_inputs_ready():
    if h is None:
        return False
    return all(gpio_pins_available.get(name, False) for name in INPUT_PINS)


_gpio_unavailable_warned = False


def teardown_gpio():
    global h, b_increase, b_playpause, b_stop
    if h is None:
        return
    try:
        for pin_name, pin in OUTPUT_PINS.items():
            if gpio_pins_available.get(pin_name):
                try:
                    lgpio.gpio_free(h, pin)
                except Exception:
                    pass
        for pin_name, pin in INPUT_PINS.items():
            if gpio_pins_available.get(pin_name):
                try:
                    lgpio.gpio_free(h, pin)
                except Exception:
                    pass
        lgpio.gpiochip_close(h)
    except Exception as e:
        print(f"Error closing GPIO chip: {e}")
    finally:
        h = None
        b_increase = None
        b_playpause = None
        b_stop = None
        for key in gpio_pins_available:
            gpio_pins_available[key] = False


def setup():
    global h, b_increase, b_playpause, b_stop, gpio_pins_available
    if h is not None and any(gpio_pins_available.values()):
        return
    project_cleanup.cleanup_stale_project_processes(log=print)
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Timer started")
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
                lgpio.gpio_claim_output(h, pin, lgpio.SET_PULL_UP)
                relay_deactivate(pin)
                gpio_pins_available[pin_name] = True
                print(f"GPIO {pin_name}({pin}) -> OK (output)")
            except Exception as e:
                gpio_pins_available[pin_name] = False
                print(f"GPIO {pin_name}({pin}) -> BUSY ({e})")

        for pin_name, pin in INPUT_PINS.items():
            try:
                lgpio.gpio_claim_input(h, pin, lgpio.SET_PULL_DOWN)
                gpio_pins_available[pin_name] = True
                print(f"GPIO {pin_name}({pin}) -> OK (input)")
            except Exception as e:
                gpio_pins_available[pin_name] = False
                print(f"GPIO {pin_name}({pin}) -> BUSY ({e})")

        print(f"GPIO summary: {sum(gpio_pins_available.values())}/{len(gpio_pins_available)} pins available")
        if not any(gpio_pins_available.values()):
            teardown_gpio()
        else:
            log_ready()
    except Exception as e:
        print(f"ERROR: GPIO initialization failed: {e}")
        teardown_gpio()


# Главная цикличная функция Raspberry
def loop(queue_main = None):
    global _gpio_unavailable_warned
    setup()

    try:
        while True:
            updateState()
            if gpio_inputs_ready():
                if b_increase.isClicked():
                    log_button(RF_PIN_NAMES[RF_INCREASE])
                    action(RF_INCREASE)
                elif b_playpause.isClicked():
                    log_button(RF_PIN_NAMES[RF_PLAYPAUSE])
                    action(RF_PLAYPAUSE)
                elif b_stop.isClicked():
                    log_button(RF_PIN_NAMES[RF_STOP])
                    action(RF_STOP)
            elif not _gpio_unavailable_warned:
                print("WARNING: GPIO inputs unavailable, radio buttons disabled (API still works)")
                _gpio_unavailable_warned = True
            try:
                if queue_main is not None:
                    signal = queue_main.get(timeout=0.1)
                    action(signal)
            except queue.Empty:
                pass
            tick(1000)
            time.sleep(0.1)
    finally:
        teardown_gpio()


# Тестирование
def test():
    action(RF_INCREASE)
    print("RF_INCREASE clicked")
    time.sleep(10)


# Действие: радио (int RF_*) или сервер (str INCREASE/PLAYPAUSE/STOP/ADD_*)
def action(signal):
    global state_starting, state_playing, state_waiting
    if isinstance(signal, int):
        if signal == RF_INCREASE:
            handle_increase("radio")
        elif signal == RF_PLAYPAUSE:
            handle_playpause("radio")
        elif signal == RF_STOP:
            handle_stop("radio")
    elif isinstance(signal, str):
        if signal == 'INCREASE':
            log_button('INCREASE (+)', 'api')
            handle_increase("api")
        elif signal == 'PLAYPAUSE':
            log_button('PLAYPAUSE', 'api')
            handle_playpause("api")
        elif signal == 'STOP':
            log_button('STOP', 'api')
            handle_stop("api")
        elif 'ADD' in signal:
            log_button(f'ADD {signal}', 'api')
            increase_count = int(int(signal.split('_')[1]) / 5)
            def increase_clicks(increase_reduce: int = 0):
                for i in range(increase_count - increase_reduce):
                    handle_increase("api")
                    time.sleep(1)
            if state_starting:
                time.sleep(5)
                increase_clicks()
                handle_playpause("api")
            elif state_playing:
                increase_clicks()
            elif state_waiting:
                handle_playpause("api")
                time.sleep(1)
                increase_clicks(1)
                handle_playpause("api")
            else:
                print("STATE: ERROR")


# Только проверка GPIO (состояние меняется в action/handle_*, не здесь)
def updateState():
    global relay_disconnected_warned
    if h is None or not gpio_pins_available['RF_INCREASE'] or not gpio_pins_available['RF_PLAYPAUSE'] or not gpio_pins_available['RF_STOP']:
        return
    try:
        if lgpio.gpio_read(h, RF_INCREASE) and lgpio.gpio_read(h, RF_PLAYPAUSE) and lgpio.gpio_read(h, RF_STOP):
            if not relay_disconnected_warned:
                print("WARNING: Radio signals look invalid (all HIGH)")
                relay_disconnected_warned = True
            return
        relay_disconnected_warned = False
    except Exception as e:
        print(f"WARNING: Error reading GPIO: {e}")


# Главная функция
if __name__ == "__main__":
    try:
        loop()
    except KeyboardInterrupt:
        finish_timer_line()
        print("Timer received KeyboardInterrupt")
    except Exception as e:
        print(f"Timer error: {e}")
    finally:
        teardown_gpio()
        print("Timer shutdown complete")
