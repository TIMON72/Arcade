import os
import queue
import lgpio
import sys
import time
from datetime import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import main as app_main

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


class TerminalCashless:
    """Сухой контакт терминала — логика как terminalCashlessProcess() в Arduino 0.21.

    Типичная схема: GPIO26 + GND (физ. 37 и 39). Замыкание тянет пин к 0.
    Нужны PULL_UP и active_high=false (покой=1, импульс=0) — как INPUT_PULLUP на Arduino.
    Короткие импульсы: lgpio alert/callback + опрос в process().
    """

    def __init__(self, pin, active_high=False, debounce_ms=0, start_delay_ms=2000):
        self.pin = pin
        self.active_high = active_high
        self.debounce_s = max(0, debounce_ms) / 1000.0
        self.start_delay_s = max(1, start_delay_ms) / 1000.0
        self.active_level = 1 if active_high else 0
        self._h = None
        self._cb = None
        self._edge_q: queue.Queue = queue.Queue()
        self._initialized = False
        self._stable_state = 0 if active_high else 1
        self._last_raw_state = self._stable_state
        self._raw_changed_at = 0.0
        self._contact_closed_at = 0.0
        self._last_contact_opened_at = 0.0
        self._pulse_count = 0
        self._start_pending = False
        self._restart_from_waiter = False

    def _on_edge(self, chip, gpio, level, timestamp):
        # level: 0/1; игнор таймаутов/прочего
        if level not in (0, 1):
            return
        self._edge_q.put((int(level), time.monotonic()))

    def setup(self, chip_handle):
        pull = lgpio.SET_PULL_DOWN if self.active_high else lgpio.SET_PULL_UP
        # alert нужен, чтобы callback ловил короткие импульсы
        try:
            lgpio.gpio_claim_alert(chip_handle, self.pin, lgpio.BOTH_EDGES, pull)
        except Exception:
            lgpio.gpio_claim_input(chip_handle, self.pin, pull)
        try:
            level = int(lgpio.gpio_read(chip_handle, self.pin))
        except Exception:
            level = 0 if self.active_high else 1
        now = time.monotonic()
        self._h = chip_handle
        self._stable_state = level
        self._last_raw_state = level
        self._raw_changed_at = now
        self._initialized = True
        self._cb = lgpio.callback(chip_handle, self.pin, lgpio.BOTH_EDGES, self._on_edge)
        print(
            f"TERM_OUT_CASHLESS READY pin={self.pin} "
            f"active_high={self.active_high} level={level} "
            f"debounce={int(self.debounce_s * 1000)}ms "
            f"start_delay={int(self.start_delay_s * 1000)}ms"
        )

    def teardown(self):
        if self._cb is not None:
            try:
                self._cb.cancel()
            except Exception:
                pass
            self._cb = None
        if self._h is not None:
            try:
                lgpio.gpio_free(self._h, self.pin)
            except Exception:
                pass
        self._h = None
        self._initialized = False
        while not self._edge_q.empty():
            try:
                self._edge_q.get_nowait()
            except queue.Empty:
                break

    def _on_pulse_closed(self):
        """Один стабильный импульс (замыкание контакта) — как в Arduino."""
        global hours, minutes, seconds, activated

        print("TERM_OUT_CASHLESS CLOSED")
        if is_arduino_mode():
            # Мост: каждый импульс = клик «+» на радиовыходе к Arduino
            self._pulse_count += 1
            self._start_pending = True
            log_button("TERM_CASHLESS", "terminal")
            do_increase("terminal")
            return

        if waited:
            # Окно ожидания: доплата. Первый импульс сбрасывает отсчёт ожидания.
            if not self._restart_from_waiter:
                self._restart_from_waiter = True
                activated = False
                hours = time_main[0]
                minutes = time_main[1]
                seconds = time_main[2]
                print("TERM_OUT_CASHLESS waiter: payment started")
            self._pulse_count += 1
            self._start_pending = True
            if hours < time_max:
                minutes += time_step
                if minutes > 59:
                    hours += 1
                    minutes = minutes % 60
            matrix_show_time()
            tick_timer.refresh()
            log_time()
            sync_state_flags()
            log_timer_state("cashless waiter pulse")
        else:
            self._pulse_count += 1
            self._start_pending = True
            log_button("TERM_CASHLESS", "terminal")
            do_increase("terminal")
            log_timer_state("cashless pulse")

    def _on_batch_complete(self):
        """Серия импульсов закончилась — автозапуск / возобновление."""
        global start, activated, waited

        print(f"TERM_OUT_CASHLESS batch complete: pulses={self._pulse_count}")
        self._pulse_count = 0

        if is_arduino_mode():
            if state_playing:
                print("TERM_OUT_CASHLESS action: timer already running, time added only")
            else:
                print("TERM_OUT_CASHLESS action: start/resume via RF → Arduino")
                do_playpause("terminal")
            return

        if self._restart_from_waiter and not is_timer_empty():
            self._restart_from_waiter = False
            start = False
            activated = False
            waited = False
            sync_state_flags()
            print("TERM_OUT_CASHLESS action: restart from waiter")
            log_button("TERM_CASHLESS PLAY", "terminal")
            do_playpause("terminal")
            tick_timer.refresh()
            log_timer_state("cashless waiter restart")
        elif not activated and not waited and not is_timer_empty():
            print("TERM_OUT_CASHLESS action: start/resume")
            log_button("TERM_CASHLESS PLAY", "terminal")
            do_playpause("terminal")
            tick_timer.refresh()
            log_timer_state("cashless start/resume")
        elif activated:
            print("TERM_OUT_CASHLESS action: timer already running, time added only")
            log_timer_state("cashless running")

    def process(self):
        if not self._initialized or self._h is None:
            return

        now = time.monotonic()
        while True:
            try:
                raw_state, _edge_at = self._edge_q.get_nowait()
            except queue.Empty:
                break
            if raw_state != self._last_raw_state:
                self._last_raw_state = raw_state
                self._raw_changed_at = now

        # Актуальный уровень (на случай пропуска края в очереди)
        try:
            live = int(lgpio.gpio_read(self._h, self.pin))
            if live != self._last_raw_state:
                self._last_raw_state = live
                self._raw_changed_at = now
        except Exception:
            pass

        if (
            self._last_raw_state != self._stable_state
            and (now - self._raw_changed_at) >= self.debounce_s
        ):
            self._stable_state = self._last_raw_state
            if self._stable_state == self.active_level:
                self._contact_closed_at = now
                self._on_pulse_closed()
            else:
                self._last_contact_opened_at = now
                duration_ms = int((now - self._contact_closed_at) * 1000)
                print(f"TERM_OUT_CASHLESS OPEN: duration={duration_ms} ms")

        if (
            self._start_pending
            and self._stable_state != self.active_level
            and (now - self._last_contact_opened_at) >= self.start_delay_s
        ):
            self._start_pending = False
            self._on_batch_complete()


# Пины GPIO и реле — из config_main.toml [gpio]
_gpio_cfg = app_main.gpio_config
_terminal_cfg = app_main.terminal_config
TIMER_MODE = app_main.timer_config.timer_mode  # "Raspberry" | "Arduino"


def is_arduino_mode() -> bool:
    return TIMER_MODE.lower() == "arduino"


def is_raspberry_mode() -> bool:
    return not is_arduino_mode()


RF_INCREASE = _gpio_cfg.rf_increase
RF_PLAYPAUSE = _gpio_cfg.rf_playpause
RF_STOP = _gpio_cfg.rf_stop
R_BUTTONS = _gpio_cfg.r_buttons
R_PLAYPAUSE = _gpio_cfg.r_playpause
R_STOP = _gpio_cfg.r_stop

# Направление пинов зависит от timer_mode (те же BCM-номера)
if is_arduino_mode():
    # init: RF → выходы (имитация радио), R_* → входы состояния с Arduino
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
else:
    # Raspberry: RF → входы пульта, R_* → выходы реле
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
h: int | None = None
b_increase: Button | None = None
b_playpause: Button | None = None
b_stop: Button | None = None
terminal_cashless: TerminalCashless | None = None

# Флаги для отслеживания каких контактов удалось выделить
gpio_pins_available = {name: False for name in {**OUTPUT_PINS, **INPUT_PINS}}

# Режим активации реле
isRelayLow = _gpio_cfg.relay_active_low
isRelayHigh = not isRelayLow
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
time_step = app_main.timer_config.time_step
time_max = 24
time_start = time_step
time_wait = app_main.timer_config.time_wait
time_reset = app_main.timer_config.time_reset

_matrix_ready = False
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

    def isReset(self, minutes):
        return (time.monotonic() - self._previous) >= minutes * 60


tick_timer = TickTimer()


def setup_matrix_display():
    global _matrix_ready
    matrix_cfg = app_main.matrix_config
    if not matrix_cfg.enabled:
        print("Matrix: disabled in config")
        return
    try:
        from modules import matrix

        matrix.setup_matrix(
            cascaded=matrix_cfg.cascaded,
            block_orientation=matrix_cfg.block_orientation,
            rotate=matrix_cfg.rotate,
            brightness=matrix_cfg.brightness,
            din=matrix_cfg.din,
            clk=matrix_cfg.clk,
            cs=matrix_cfg.cs,
            blocks_reverse=matrix_cfg.blocks_reverse,
        )
        if matrix_cfg.test_on_start:
            matrix.run_self_test()
        matrix.start_scrolling_text(matrix_cfg.text_display, speed=matrix_cfg.scroll_speed)
        _matrix_ready = True
        print(
            f"Matrix: MAX7219 initialized (bitbang, "
            f"DIN/CLK/CS={matrix_cfg.din}/{matrix_cfg.clk}/{matrix_cfg.cs}, "
            f"cascaded={matrix_cfg.cascaded})"
        )
    except Exception as error:
        _matrix_ready = False
        print(f"WARNING: Matrix init failed: {error}")


def matrix_show_time():
    if not _matrix_ready:
        return
    from modules import matrix

    matrix.print_time(hours, minutes, seconds)


def matrix_show_waiting():
    if not _matrix_ready:
        return
    from modules import matrix

    matrix.print_waiting_time(seconds)


def matrix_show_text(label):
    if not _matrix_ready:
        return
    from modules import matrix

    matrix.print_text(label)


def matrix_show_start():
    if _matrix_ready:
        from modules import matrix

        matrix.print_start(time_start)
        return
    _console_start_countdown()


def matrix_resume_scroll():
    if not _matrix_ready:
        return
    from modules import matrix

    matrix_cfg = app_main.matrix_config
    matrix.start_scrolling_text(matrix_cfg.text_display, speed=matrix_cfg.scroll_speed)


def matrix_scroll_tick():
    if not _matrix_ready:
        return
    from modules import matrix

    matrix.scroll_tick()


def update_matrix_idle():
    if not _matrix_ready or activated:
        return
    if (
        hours == time_main[0]
        and minutes == time_main[1]
        and seconds == time_main[2]
    ):
        matrix_scroll_tick()
    elif tick_timer.isReset(time_reset):
        handle_stop("timer")


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


def _console_start_countdown():
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
    matrix_show_time()
    tick_timer.refresh()
    log_time()


def handle_playpause(source="radio"):
    global start, activated, waited, hours, minutes, seconds
    finish_timer_line()
    if not start:
        if is_timer_empty():
            print("BTN: PLAYPAUSE ignored: timer not set (00:00:00)")
            return
        matrix_show_start()
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
        matrix_show_text("ПАУЗА")
        print("STATE: PAUSE")
        activated = False
        sync_state_flags()
        tick_timer.refresh()
    elif not activated and not waited:
        matrix_show_start()
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
        matrix_show_waiting()
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
        matrix_show_time()
        log_time()
        sync_state_flags()
        log_ready()
        matrix_resume_scroll()


def handle_stop(source="radio"):
    global start, activated, waited, hours, minutes, seconds
    finish_timer_line()
    print(f"RELAY: R_STOP({R_STOP}) click")
    relay_click(R_STOP)
    print(f"RELAY: R_BUTTONS({R_BUTTONS}) deactivate")
    relay_deactivate(R_BUTTONS)
    matrix_show_text("КОНЕЦ")
    start = False
    activated = False
    waited = False
    hours = time_main[0]
    minutes = time_main[1]
    seconds = time_main[2]
    if _matrix_ready:
        from modules import matrix

        time.sleep(5)
        matrix.clear()
    log_time()
    sync_state_flags()
    log_ready()
    matrix_resume_scroll()


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


def rf_click(rf_pin: int):
    """Имитация нажатия радиокнопки (выход HIGH ~1 с) — режим Arduino / init."""
    if h is None:
        print(f"RF: skip click pin={rf_pin} (GPIO not open)")
        return
    pin_name = PIN_TO_OUTPUT_NAME.get(rf_pin)
    if pin_name and not gpio_pins_available.get(pin_name):
        print(f"RF: skip click {pin_name}({rf_pin}) (not available)")
        return
    label = RF_PIN_NAMES.get(rf_pin, str(rf_pin))
    print(f"RF: {label}({rf_pin}) click")
    lgpio.gpio_write(h, rf_pin, 1)
    time.sleep(1)
    lgpio.gpio_write(h, rf_pin, 0)


def do_increase(source="api"):
    if is_arduino_mode():
        if source != "terminal":
            log_button("INCREASE (+)", source)
        rf_click(RF_INCREASE)
    else:
        handle_increase(source)


def do_playpause(source="api"):
    if is_arduino_mode():
        if source != "terminal":
            log_button("PLAYPAUSE", source)
        rf_click(RF_PLAYPAUSE)
    else:
        handle_playpause(source)


def do_stop(source="api"):
    if is_arduino_mode():
        if source != "terminal":
            log_button("STOP", source)
        rf_click(RF_STOP)
    else:
        handle_stop(source)


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
        matrix_show_waiting()
        sync_state_flags()
        log_timer_state("waiting")
        tick_timer.refresh()
    else:
        activated = False
        handle_stop("timer")


def tick(delay_ms=1000):
    global hours, minutes, seconds, activated, waited
    if not activated:
        return
    if not tick_timer.isTicked(delay_ms):
        return
    if waited:
        matrix_show_waiting()
    else:
        matrix_show_time()
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


def gpio_inputs_ready():
    if h is None:
        return False
    return all(gpio_pins_available.get(name, False) for name in INPUT_PINS)


_gpio_unavailable_warned = False


def teardown_gpio():
    global h, b_increase, b_playpause, b_stop, terminal_cashless
    if h is None:
        return
    try:
        if terminal_cashless is not None:
            terminal_cashless.teardown()
            terminal_cashless = None
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
        terminal_cashless = None
        for key in gpio_pins_available:
            gpio_pins_available[key] = False


def setup():
    global h, b_increase, b_playpause, b_stop, gpio_pins_available, terminal_cashless
    if h is not None and any(gpio_pins_available.values()):
        return
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Timer started")
    print(f"Config: timer_mode={TIMER_MODE}, time_step={time_step} min, time_wait={time_wait} sec, time_reset={time_reset} min")
    print(
        "GPIO: "
        f"RF_INCREASE={RF_INCREASE}, RF_PLAYPAUSE={RF_PLAYPAUSE}, RF_STOP={RF_STOP}, "
        f"R_BUTTONS={R_BUTTONS}, R_PLAYPAUSE={R_PLAYPAUSE}, R_STOP={R_STOP}, "
        f"relay_active_low={isRelayLow}"
    )
    if is_arduino_mode():
        print("Mode Arduino: RF pins = OUTPUT (radio sim), R_* pins = INPUT (state from Arduino)")
    else:
        print("Mode Raspberry: RF pins = INPUT (remote), R_* pins = OUTPUT (relays)")
    print(
        "Terminal: "
        f"enabled={_terminal_cfg.enabled}, pin={_terminal_cfg.pin}, "
        f"active_high={_terminal_cfg.active_high}, "
        f"debounce_ms={_terminal_cfg.debounce_ms}, "
        f"start_delay_ms={_terminal_cfg.start_delay_ms}"
    )
    if is_raspberry_mode():
        setup_matrix_display()
    else:
        print("Matrix: skipped (timer_mode=Arduino — display on Arduino)")
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

        if is_raspberry_mode():
            b_increase = Button(h, RF_INCREASE)
            b_playpause = Button(h, RF_PLAYPAUSE)
            b_stop = Button(h, RF_STOP)
        else:
            b_increase = None
            b_playpause = None
            b_stop = None

        for pin_name, pin in OUTPUT_PINS.items():
            try:
                if is_arduino_mode():
                    # Радиоимитация: покой LOW
                    lgpio.gpio_claim_output(h, pin, 0)
                else:
                    lgpio.gpio_claim_output(h, pin, lgpio.SET_PULL_UP)
                    relay_deactivate(pin)
                gpio_pins_available[pin_name] = True
                print(f"GPIO {pin_name}({pin}) -> OK (output)")
            except Exception as e:
                gpio_pins_available[pin_name] = False
                print(f"GPIO {pin_name}({pin}) -> BUSY ({e})")

        for pin_name, pin in INPUT_PINS.items():
            try:
                if is_arduino_mode():
                    lgpio.gpio_claim_input(h, pin)
                else:
                    lgpio.gpio_claim_input(h, pin, lgpio.SET_PULL_DOWN)
                gpio_pins_available[pin_name] = True
                print(f"GPIO {pin_name}({pin}) -> OK (input)")
            except Exception as e:
                gpio_pins_available[pin_name] = False
                print(f"GPIO {pin_name}({pin}) -> BUSY ({e})")

        if _terminal_cfg.enabled:
            try:
                terminal_cashless = TerminalCashless(
                    pin=_terminal_cfg.pin,
                    active_high=_terminal_cfg.active_high,
                    debounce_ms=_terminal_cfg.debounce_ms,
                    start_delay_ms=_terminal_cfg.start_delay_ms,
                )
                terminal_cashless.setup(h)
                print(f"GPIO TERM_CASHLESS({_terminal_cfg.pin}) -> OK (input/alert)")
            except Exception as e:
                terminal_cashless = None
                print(f"GPIO TERM_CASHLESS({_terminal_cfg.pin}) -> BUSY ({e})")
        else:
            print("Terminal cashless: disabled in config")

        print(f"GPIO summary: {sum(gpio_pins_available.values())}/{len(gpio_pins_available)} pins available")
        if not any(gpio_pins_available.values()) and terminal_cashless is None:
            teardown_gpio()
        else:
            log_ready()
    except Exception as e:
        print(f"ERROR: GPIO initialization failed: {e}")
        teardown_gpio()


# Главная цикличная функция
def loop(queue_main = None):
    global _gpio_unavailable_warned
    setup()

    try:
        while True:
            updateState()
            if terminal_cashless is not None:
                terminal_cashless.process()
            if is_raspberry_mode():
                if (
                    gpio_inputs_ready()
                    and b_increase is not None
                    and b_playpause is not None
                    and b_stop is not None
                ):
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
                    signal = queue_main.get(timeout=0.02)
                    action(signal)
            except queue.Empty:
                pass
            if is_raspberry_mode():
                update_matrix_idle()
                tick(1000)
            time.sleep(0.02)
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
            do_increase("radio")
        elif signal == RF_PLAYPAUSE:
            do_playpause("radio")
        elif signal == RF_STOP:
            do_stop("radio")
    elif isinstance(signal, str):
        if signal == 'INCREASE':
            do_increase("api")
        elif signal == 'PLAYPAUSE':
            do_playpause("api")
        elif signal == 'STOP':
            do_stop("api")
        elif 'ADD' in signal:
            log_button(f'ADD {signal}', 'api')
            increase_count = int(int(signal.split('_')[1]) / 5)

            def increase_clicks(increase_reduce: int = 0):
                for _ in range(increase_count - increase_reduce):
                    do_increase("api")
                    time.sleep(1)

            if state_starting:
                time.sleep(5)
                increase_clicks()
                do_playpause("api")
            elif state_playing:
                increase_clicks()
            elif state_waiting:
                do_playpause("api")
                time.sleep(1)
                increase_clicks(1)
                do_playpause("api")
            else:
                print("STATE: ERROR")


def update_state_from_relays():
    """Режим Arduino: состояния по входам R_* (как updateState в init)."""
    global state_starting, state_playing, state_waiting
    if h is None:
        return
    if not all(gpio_pins_available.get(name, False) for name in ('R_BUTTONS', 'R_PLAYPAUSE', 'R_STOP')):
        return
    try:
        buttons_state = lgpio.gpio_read(h, R_BUTTONS)
        playpause_state = lgpio.gpio_read(h, R_PLAYPAUSE)
        stop_state = lgpio.gpio_read(h, R_STOP)
    except Exception as e:
        print(f"WARNING: Error reading relay state GPIO: {e}")
        return

    if isRelayLow:
        buttons_state = not buttons_state
        playpause_state = not playpause_state
        stop_state = not stop_state

    # Нажаты все кнопки? : Реле не подключено
    if buttons_state and playpause_state and stop_state:
        if state_starting or state_playing or state_waiting:
            print("STATE: ERROR")
        state_starting = False
        state_playing = False
        state_waiting = False
    elif not state_starting and stop_state:
        print("STATE: STARTING")
        state_starting = True
        state_playing = False
        state_waiting = False
    elif not state_playing and not buttons_state and playpause_state:
        print("STATE: PLAYING")
        state_starting = False
        state_playing = True
        state_waiting = False
    elif not state_waiting and buttons_state and playpause_state:
        print("STATE: WAITING")
        state_starting = False
        state_playing = False
        state_waiting = True


def update_state_radio_watchdog():
    """Режим Raspberry: предупреждение, если все RF-входы странно HIGH."""
    global relay_disconnected_warned
    if h is None or not gpio_pins_available.get('RF_INCREASE') or not gpio_pins_available.get('RF_PLAYPAUSE') or not gpio_pins_available.get('RF_STOP'):
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


def updateState():
    if is_arduino_mode():
        update_state_from_relays()
    else:
        update_state_radio_watchdog()


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
