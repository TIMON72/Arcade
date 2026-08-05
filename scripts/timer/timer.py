import os
import queue
import lgpio
import sys
import time
import tomllib
from dataclasses import dataclass

_TIMER_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_TIMER_DIR)
_MAIN_DIR = os.path.join(_SCRIPTS_ROOT, "main")
_CONFIG_PATH = os.path.join(_TIMER_DIR, "config.toml")
if _TIMER_DIR not in sys.path:
    sys.path.insert(0, _TIMER_DIR)
if _MAIN_DIR not in sys.path:
    sys.path.insert(0, _MAIN_DIR)

from main import log, CmdListener


# ---------------------------------------------------------------------------
# config.toml — один файл (как у tvon/main)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TimerConfig:
    time_step: int = 5
    time_wait: int = 60
    time_reset: int = 5
    timer_mode: str = "Raspberry"
    cmd_socket: str = "/var/run/arcade-timer.sock"


@dataclass(frozen=True)
class GpioConfig:
    rf_increase: int = 5
    rf_playpause: int = 6
    rf_stop: int = 13
    r_buttons: int = 17
    r_playpause: int = 27
    r_stop: int = 22
    relay_active_low: bool = True


@dataclass(frozen=True)
class TerminalConfig:
    enabled: bool = True
    pin: int = 26
    active_high: bool = False
    debounce_ms: int = 0
    start_delay_ms: int = 2000


@dataclass(frozen=True)
class MatrixConfig:
    enabled: bool = True
    brightness: int = 7
    scroll_speed: int = 7
    text_display: str = "АРЕНДА: т. +79233549295"
    din: int = 10
    clk: int = 11
    cs: int = 8
    cascaded: int = 4
    block_orientation: int = 90
    rotate: int = 2
    blocks_reverse: bool = True
    test_on_start: bool = True


def _read_positive_int(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value <= 0:
        raise ValueError(f"{key} must be greater than 0")
    return value


def _read_non_negative_int(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < 0:
        raise ValueError(f"{key} must be >= 0")
    return value


def _read_string(section: dict, key: str, default: str) -> str:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _read_bool(section: dict, key: str, default: bool) -> bool:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _read_bcm_pin(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < 0 or value > 53:
        raise ValueError(f"{key} must be BCM pin 0..53")
    return value


def _read_brightness(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < 0 or value > 15:
        raise ValueError(f"{key} must be 0..15")
    return value


def _read_block_orientation(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value not in (0, 90, -90, 180):
        raise ValueError(f"{key} must be one of 0, 90, -90, 180")
    return value


def _read_rotate(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value not in (0, 1, 2, 3):
        raise ValueError(f"{key} must be 0..3")
    return value


def _read_timer_mode(section: dict, default: str = "Raspberry") -> str:
    raw = section.get("timer_mode", section.get("mode", default))
    if not isinstance(raw, str):
        raise ValueError("timer_mode must be a string")
    normalized = raw.strip().lower()
    if normalized in {"raspberry", "rpi", "pi"}:
        return "Raspberry"
    if normalized == "arduino":
        return "Arduino"
    raise ValueError(f"timer_mode must be Raspberry or Arduino, got {raw!r}")


def _load_config_data() -> dict:
    if not os.path.isfile(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, "rb") as config_file:
        data = tomllib.load(config_file)
    if not isinstance(data, dict):
        raise ValueError("config root must be a table")
    return data


def load_timer_config(data: dict | None = None) -> TimerConfig:
    defaults = TimerConfig()
    section = (data if data is not None else _load_config_data()).get("timer", {})
    if not isinstance(section, dict):
        raise ValueError("[timer] must be a table")
    return TimerConfig(
        time_step=_read_positive_int(section, "time_step", defaults.time_step),
        time_wait=_read_positive_int(section, "time_wait", defaults.time_wait),
        time_reset=_read_positive_int(section, "time_reset", defaults.time_reset),
        timer_mode=_read_timer_mode(section, defaults.timer_mode),
        cmd_socket=_read_string(section, "cmd_socket", defaults.cmd_socket),
    )


def load_gpio_config(data: dict | None = None) -> GpioConfig:
    defaults = GpioConfig()
    section = (data if data is not None else _load_config_data()).get("gpio", {})
    if not isinstance(section, dict):
        raise ValueError("[gpio] must be a table")
    return GpioConfig(
        rf_increase=_read_bcm_pin(section, "rf_increase", defaults.rf_increase),
        rf_playpause=_read_bcm_pin(section, "rf_playpause", defaults.rf_playpause),
        rf_stop=_read_bcm_pin(section, "rf_stop", defaults.rf_stop),
        r_buttons=_read_bcm_pin(section, "r_buttons", defaults.r_buttons),
        r_playpause=_read_bcm_pin(section, "r_playpause", defaults.r_playpause),
        r_stop=_read_bcm_pin(section, "r_stop", defaults.r_stop),
        relay_active_low=_read_bool(section, "relay_active_low", defaults.relay_active_low),
    )


def load_terminal_config(data: dict | None = None) -> TerminalConfig:
    defaults = TerminalConfig()
    section = (data if data is not None else _load_config_data()).get("terminal", {})
    if not isinstance(section, dict):
        raise ValueError("[terminal] must be a table")
    return TerminalConfig(
        enabled=_read_bool(section, "enabled", defaults.enabled),
        pin=_read_bcm_pin(section, "pin", defaults.pin),
        active_high=_read_bool(section, "active_high", defaults.active_high),
        debounce_ms=_read_non_negative_int(section, "debounce_ms", defaults.debounce_ms),
        start_delay_ms=_read_positive_int(section, "start_delay_ms", defaults.start_delay_ms),
    )


def load_matrix_config(data: dict | None = None) -> MatrixConfig:
    defaults = MatrixConfig()
    section = (data if data is not None else _load_config_data()).get("matrix", {})
    if not isinstance(section, dict):
        raise ValueError("[matrix] must be a table")
    return MatrixConfig(
        enabled=_read_bool(section, "enabled", defaults.enabled),
        brightness=_read_brightness(section, "brightness", defaults.brightness),
        scroll_speed=_read_positive_int(section, "scroll_speed", defaults.scroll_speed),
        text_display=_read_string(section, "text_display", defaults.text_display),
        din=_read_bcm_pin(section, "din", defaults.din),
        clk=_read_bcm_pin(section, "clk", defaults.clk),
        cs=_read_bcm_pin(section, "cs", defaults.cs),
        cascaded=_read_positive_int(section, "cascaded", defaults.cascaded),
        block_orientation=_read_block_orientation(
            section, "block_orientation", defaults.block_orientation
        ),
        rotate=_read_rotate(section, "rotate", defaults.rotate),
        blocks_reverse=_read_bool(section, "blocks_reverse", defaults.blocks_reverse),
        test_on_start=_read_bool(section, "test_on_start", defaults.test_on_start),
    )


_config_data = _load_config_data()
timer_config = load_timer_config(_config_data)
gpio_config = load_gpio_config(_config_data)
terminal_config = load_terminal_config(_config_data)
matrix_config = load_matrix_config(_config_data)


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

        finish_timer_line()
        log("TERM_OUT_CASHLESS: pulse")

        if is_arduino_mode():
            # В WAITING Increase до Play глотается — шлём пакетом в batch
            self._pulse_count += 1
            self._start_pending = True
            return

        if waited:
            # Окно ожидания: доплата. Первый импульс сбрасывает отсчёт ожидания.
            if not self._restart_from_waiter:
                self._restart_from_waiter = True
                activated = False
                hours = time_main[0]
                minutes = time_main[1]
                seconds = time_main[2]
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
        else:
            self._pulse_count += 1
            self._start_pending = True
            do_increase("terminal")
            sync_state_flags()

    def _on_batch_complete(self):
        """Серия импульсов закончилась — автозапуск / возобновление."""
        global start, activated, waited

        pulses = self._pulse_count
        self._pulse_count = 0

        if is_arduino_mode():
            if pulses > 0:
                arduino_pay(pulses)
            return

        if self._restart_from_waiter and not is_timer_empty():
            self._restart_from_waiter = False
            start = False
            activated = False
            waited = False
            sync_state_flags()
            do_playpause("terminal")
            tick_timer.refresh()
        elif not activated and not waited and not is_timer_empty():
            do_playpause("terminal")
            tick_timer.refresh()

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

        if (
            self._start_pending
            and self._stable_state != self.active_level
            and (now - self._last_contact_opened_at) >= self.start_delay_s
        ):
            self._start_pending = False
            self._on_batch_complete()


# Пины GPIO и реле — из config.toml [gpio]
_gpio_cfg = gpio_config
_terminal_cfg = terminal_config
TIMER_MODE = timer_config.timer_mode  # "Raspberry" | "Arduino"


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
# WAITING = ждём продолжения (лог один); внутри: пауза vs окно waited Arduino
# STOPPING → глухое окно → READY; оплата HTTP/терминал за окно копится
state_starting = True
state_playing = False
state_waiting = False
state_stopping = False
# True = post-game окно time_wait на Arduino (waited): RF «+» глотается
state_waiter = False
# Как start / activated / waited в Raspberry-режиме
start = False
activated = False
waited = False
hours = 0
minutes = 0
seconds = 0
time_main = [0, 0, 0]
time_step = timer_config.time_step
time_max = 24
time_start = time_step
time_wait = timer_config.time_wait
time_reset = timer_config.time_reset

_matrix_ready = False
relay_disconnected_warned = False
_last_logged_state = None
_timer_line_active = False

# Arduino глух только на STOPPING/«КОНЕЦ» (~6с) — копим оплату в очереди
arduino_busy_until = 0.0
pending_plus = 0
pending_play = False
# Наш последний RF PLAYPAUSE — отличить паузу (мы) от waiter (конец времени на Arduino)
rf_playpause_sent_at = 0.0
RF_PLAY_GRACE_S = 2.5

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
    matrix_cfg = matrix_config
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

    matrix_cfg = matrix_config
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


def machine_state_label():
    if state_stopping:
        return "STOPPING"
    if state_waiting:
        return "WAITING"
    if state_playing:
        return "PLAYING"
    if state_starting:
        return "READY"
    return "UNKNOWN"


def log_state(name, reason=""):
    global _last_logged_state
    finish_timer_line()
    if name == _last_logged_state and not reason:
        return
    _last_logged_state = name
    if reason:
        log(f"STATE: {name} ({reason})")
    else:
        log(f"STATE: {name}")


def log_timer_state(reason=""):
    log_state(machine_state_label(), reason)


def log_ready():
    log_state("READY")


def log_act(command, source, goal="", from_state=""):
    finish_timer_line()
    from_state = from_state or machine_state_label()
    parts = [f"ACT: {command} ({source})"]
    if goal:
        parts.append(f"→ {goal}")
    if from_state:
        parts.append(f"[from {from_state}]")
    log(" ".join(parts))


def log_button(name, source="radio"):
    log_act(name, source)


def log_time():
    finish_timer_line()
    log(f"TIME: {hours:02d}:{minutes:02d}:{seconds:02d}")


def is_timer_empty():
    return hours == 0 and minutes == 0 and seconds == 0


def set_machine_state(name=None, starting=False, playing=False, waiting=False):
    global state_starting, state_playing, state_waiting, state_stopping, state_waiter
    state_starting = starting
    state_playing = playing
    state_waiting = waiting
    if not waiting:
        state_waiter = False
    if name == "STOPPING":
        state_stopping = True
    elif name is not None:
        state_stopping = False
    if name:
        log_state(name)


def sync_state_flags():
    if not start:
        set_machine_state(starting=True)
    elif activated and not waited:
        set_machine_state(playing=True)
    else:
        set_machine_state(waiting=True)


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
            log("ACT: PLAYPAUSE ignored — timer 00:00:00")
            return
        matrix_show_start()
        log(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
        relay_click(R_PLAYPAUSE)
        log(f"RELAY: R_BUTTONS({R_BUTTONS}) activate")
        relay_activate(R_BUTTONS)
        start = True
        activated = True
        waited = False
        sync_state_flags()
        log_timer_state("play")
        tick_timer.refresh()
    elif activated and not waited:
        log(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
        relay_click(R_PLAYPAUSE)
        log(f"RELAY: R_BUTTONS({R_BUTTONS}) deactivate")
        relay_deactivate(R_BUTTONS)
        matrix_show_text("ПАУЗА")
        activated = False
        sync_state_flags()
        log_timer_state()
        tick_timer.refresh()
    elif not activated and not waited:
        matrix_show_start()
        log(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
        relay_click(R_PLAYPAUSE)
        log(f"RELAY: R_BUTTONS({R_BUTTONS}) activate")
        relay_activate(R_BUTTONS)
        activated = True
        sync_state_flags()
        log_timer_state("play")
        tick_timer.refresh()
    elif waited and not activated:
        log(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
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
    log(f"RELAY: R_STOP({R_STOP}) click")
    relay_click(R_STOP)
    log(f"RELAY: R_BUTTONS({R_BUTTONS}) deactivate")
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
    """Имитация нажатия радиокнопки (выход HIGH ~1 с) — режим Arduino."""
    if h is None:
        log(f"RF: skip click pin={rf_pin} (GPIO not open)")
        return
    pin_name = PIN_TO_OUTPUT_NAME.get(rf_pin)
    if pin_name and not gpio_pins_available.get(pin_name):
        log(f"RF: skip click {pin_name}({rf_pin}) (not available)")
        return
    short = {RF_INCREASE: "INCREASE", RF_PLAYPAUSE: "PLAYPAUSE", RF_STOP: "STOP"}.get(
        rf_pin, RF_PIN_NAMES.get(rf_pin, str(rf_pin))
    )
    finish_timer_line()
    log(f"RF: {short}({rf_pin}) clicked")
    lgpio.gpio_write(h, rf_pin, 1)
    time.sleep(1)
    lgpio.gpio_write(h, rf_pin, 0)


def arduino_busy():
    return time.monotonic() < arduino_busy_until


def arduino_hold(sec=6.0):
    """Arduino глух ~6с только на STOPPING/«КОНЕЦ» — копим оплату."""
    global arduino_busy_until
    arduino_busy_until = time.monotonic() + sec


def begin_stopping():
    """STOPPING + глухое окно; оплата копится до READY."""
    set_machine_state("STOPPING", starting=True)
    arduino_hold()


def rf_burst(pin, count, gap=0.3):
    for _ in range(max(0, count)):
        rf_click(pin)
        time.sleep(gap)


def arduino_pay(steps):
    """Оплата → RF. 1 шаг = 1 импульс / +5 мин на Arduino (конфиг там).

    Пауза (WAITING, не waiter): Increase×N → Play.
    Окно waited Arduino (60с после конца): Play → Increase×(N-1) → Play
      (RF «+» при waited игнорируется; первый Play только снимает waited и
       на прошивке 1.x даёт +time_step без старта реле — см. Arcade_Timer_Arduino).
    PLAYING: Increase×N.
    READY: Increase×N → Play.
    Глухое окно только STOPPING — оплату копим, после READY сбрасываем с Play.
    """
    global pending_plus, pending_play, state_playing, state_waiting, state_waiter
    global rf_playpause_sent_at
    if steps <= 0:
        return
    if arduino_busy() or state_stopping:
        pending_plus += steps
        pending_play = True
        log(f"ACT: +{steps * 5} queued (Arduino busy / STOPPING)")
        return
    if state_waiting and state_waiter:
        rest = max(0, steps - 1)
        log(f"ACT: pay +{steps * 5} (waiter → PLAY +{rest * 5} PLAY)")
        rf_click(RF_PLAYPAUSE)
        rf_playpause_sent_at = time.monotonic()
        if rest > 0:
            time.sleep(0.3)
            rf_burst(RF_INCREASE, rest)
        time.sleep(0.3)
        rf_click(RF_PLAYPAUSE)
        rf_playpause_sent_at = time.monotonic()
        state_waiting = False
        state_waiter = False
        state_playing = True
        log_state("PLAYING")
        return
    if state_waiting:
        log(f"ACT: pay +{steps * 5} (pause → + then PLAY)")
        rf_burst(RF_INCREASE, steps)
        rf_click(RF_PLAYPAUSE)
        rf_playpause_sent_at = time.monotonic()
        state_waiting = False
        state_playing = True
        log_state("PLAYING")
        return
    if state_playing:
        log(f"ACT: pay +{steps * 5} (playing)")
        rf_burst(RF_INCREASE, steps)
        return
    log(f"ACT: pay +{steps * 5} (idle → + then PLAY)")
    rf_burst(RF_INCREASE, steps)
    rf_click(RF_PLAYPAUSE)
    rf_playpause_sent_at = time.monotonic()


def flush_arduino():
    """После глухого окна: STOPPING→READY, затем накопленные + / Play."""
    global pending_plus, pending_play, state_stopping
    if arduino_busy():
        return
    if state_stopping:
        state_stopping = False
        set_machine_state(starting=True)
        log_ready()
        if pending_plus > 0:
            pending_play = True
    n, play = pending_plus, pending_play
    if n <= 0 and not play:
        return
    pending_plus = 0
    pending_play = False
    if n > 0:
        log(f"ACT: flush +{n * 5} min")
    rf_burst(RF_INCREASE, n, gap=0.2)
    if play:
        rf_click(RF_PLAYPAUSE)


def do_increase(source="api"):
    global pending_plus
    if is_arduino_mode():
        if arduino_busy():
            pending_plus += 1
        else:
            rf_click(RF_INCREASE)
    else:
        handle_increase(source)


def do_playpause(source="api"):
    global pending_play, state_waiting, state_playing, state_waiter, rf_playpause_sent_at
    if is_arduino_mode():
        if arduino_busy():
            pending_play = True
            return
        # Play в PLAYING → пауза (WAITING); waiter → Play×2 (снятие waited + START)
        if state_playing and not state_waiting:
            state_playing = False
            state_waiting = True
            state_waiter = False
            log_state("WAITING")
            rf_click(RF_PLAYPAUSE)
            rf_playpause_sent_at = time.monotonic()
        elif state_waiting and state_waiter:
            rf_click(RF_PLAYPAUSE)
            time.sleep(0.3)
            rf_click(RF_PLAYPAUSE)
            rf_playpause_sent_at = time.monotonic()
            state_waiting = False
            state_waiter = False
            state_playing = True
            log_state("PLAYING")
        elif state_waiting:
            state_waiting = False
            state_playing = True
            log_state("PLAYING")
            rf_click(RF_PLAYPAUSE)
            rf_playpause_sent_at = time.monotonic()
        else:
            rf_click(RF_PLAYPAUSE)
            rf_playpause_sent_at = time.monotonic()
    else:
        handle_playpause(source)


def do_stop(source="api"):
    if is_arduino_mode():
        rf_click(RF_STOP)
        if source == "setup":
            # Сброс при старте Pi: hold есть, READY уже логирует setup
            arduino_hold()
        else:
            begin_stopping()
    else:
        handle_stop(source)


def _on_countdown_finished():
    global activated, waited, seconds
    finish_timer_line()
    if not waited:
        waited = True
        activated = False
        log(f"RELAY: R_PLAYPAUSE({R_PLAYPAUSE}) click")
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
    log("Timer started")
    log(
        f"Config: timer_mode={TIMER_MODE}, time_step={time_step} min, "
        f"time_wait={time_wait} sec, time_reset={time_reset} min"
    )
    log(
        "GPIO: "
        f"RF_INCREASE={RF_INCREASE}, RF_PLAYPAUSE={RF_PLAYPAUSE}, RF_STOP={RF_STOP}, "
        f"R_BUTTONS={R_BUTTONS}, R_PLAYPAUSE={R_PLAYPAUSE}, R_STOP={R_STOP}, "
        f"relay_active_low={isRelayLow}"
    )
    if is_arduino_mode():
        log("Mode Arduino: RF pins = OUTPUT (radio sim), R_* pins = INPUT (state from Arduino)")
    else:
        log("Mode Raspberry: RF pins = INPUT (remote), R_* pins = OUTPUT (relays)")
    log(
        "Terminal: "
        f"enabled={_terminal_cfg.enabled}, pin={_terminal_cfg.pin}, "
        f"active_high={_terminal_cfg.active_high}, "
        f"debounce_ms={_terminal_cfg.debounce_ms}, "
        f"start_delay_ms={_terminal_cfg.start_delay_ms}"
    )
    if is_raspberry_mode():
        setup_matrix_display()
    else:
        log("Matrix: skipped (timer_mode=Arduino — display on Arduino)")
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
            if is_arduino_mode() and gpio_pins_available.get('RF_STOP'):
                # Сброс сессии на Arduino при старте Pi (отладка / рестарт сервиса)
                log_act("STOP", "setup", "reset")
                do_stop("setup")
                set_machine_state(starting=True)
            log_ready()
    except Exception as e:
        log(f"ERROR: GPIO initialization failed: {e}")
        teardown_gpio()


def loop(queue_main=None):
    global _gpio_unavailable_warned
    setup()
    listener = CmdListener(timer_config.cmd_socket)
    try:
        listener.setup()
        log(f"Timer cmd socket: {timer_config.cmd_socket}")
    except OSError as e:
        log(f"WARNING: cmd socket bind failed: {e}")
        listener = None

    try:
        while True:
            updateState()
            if is_arduino_mode():
                flush_arduino()
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
                        log_act("INCREASE", "radio", f"+{time_step}")
                        action(RF_INCREASE)
                    elif b_playpause.isClicked():
                        log_act("PLAYPAUSE", "radio")
                        action(RF_PLAYPAUSE)
                    elif b_stop.isClicked():
                        log_act("STOP", "radio", "reset")
                        action(RF_STOP)
                elif not _gpio_unavailable_warned:
                    log("WARNING: GPIO inputs unavailable, radio buttons disabled (API still works)")
                    _gpio_unavailable_warned = True
            if listener is not None:
                signal = listener.poll()
                if signal:
                    action(signal)
            # Legacy: очередь из старого multiprocessing main
            if queue_main is not None:
                try:
                    signal = queue_main.get(timeout=0.02)
                    action(signal)
                except queue.Empty:
                    pass
            if is_raspberry_mode():
                update_matrix_idle()
                tick(1000)
            time.sleep(0.02)
    finally:
        if listener is not None:
            listener.close()
        teardown_gpio()


# Тестирование
def test():
    action(RF_INCREASE)
    print("RF_INCREASE clicked")
    time.sleep(10)


# Действие: радио (int RF_*) или сервер (str INCREASE/PLAYPAUSE/STOP/ADD_*)
def action(signal):
    global pending_plus, pending_play
    if isinstance(signal, int):
        if signal == RF_INCREASE:
            do_increase("radio")
        elif signal == RF_PLAYPAUSE:
            do_playpause("radio")
        elif signal == RF_STOP:
            do_stop("radio")
    elif isinstance(signal, str):
        if signal is None or signal == "" or signal == "None":
            return
        if signal == 'INCREASE':
            log_act("INCREASE", "api", f"+{time_step}")
            do_increase("api")
        elif signal == 'PLAYPAUSE':
            log_act("PLAYPAUSE", "api")
            do_playpause("api")
        elif signal == 'STOP':
            log_act("STOP", "api", "reset")
            do_stop("api")
        elif 'ADD' in signal:
            minutes = int(signal.split('_')[1])
            increase_count = int(minutes / 5)
            from_state = machine_state_label()

            def increase_clicks(reduce=0):
                for _ in range(increase_count - reduce):
                    do_increase("api")
                    time.sleep(1)

            # STOPPING / глухое окно: копим + и Play до READY
            if is_arduino_mode() and (arduino_busy() or state_stopping):
                pending_plus += increase_count
                pending_play = True
                log_act(signal, "api", f"+{minutes} queued", from_state)
            elif state_starting:
                log_act(signal, "api", f"+{minutes} then PLAY", from_state)
                if is_arduino_mode():
                    arduino_pay(increase_count)
                else:
                    increase_clicks()
                    do_playpause("api")
            elif state_playing:
                log_act(signal, "api", f"+{minutes}", from_state)
                if is_arduino_mode():
                    arduino_pay(increase_count)
                else:
                    increase_clicks()
            elif state_waiting:
                log_act(signal, "api", f"+{minutes} then PLAY", from_state)
                if is_arduino_mode():
                    arduino_pay(increase_count)
                else:
                    increase_clicks()
                    do_playpause("api")
            else:
                log_act(signal, "api", "skip", from_state)
                log_state("ERROR")


def update_state_from_relays():
    """Режим Arduino: состояния по входам R_*."""
    global state_waiter
    if h is None:
        return
    if not all(gpio_pins_available.get(name, False) for name in ('R_BUTTONS', 'R_PLAYPAUSE', 'R_STOP')):
        return
    try:
        buttons_state = lgpio.gpio_read(h, R_BUTTONS)
        playpause_state = lgpio.gpio_read(h, R_PLAYPAUSE)
        stop_state = lgpio.gpio_read(h, R_STOP)
    except Exception as e:
        log(f"WARNING: Error reading relay state GPIO: {e}")
        return

    if isRelayLow:
        buttons_state = not buttons_state
        playpause_state = not playpause_state
        stop_state = not stop_state

    # Все «активны» → реле не подключено
    if buttons_state and playpause_state and stop_state:
        if state_starting or state_playing or state_waiting:
            set_machine_state("ERROR")
        else:
            set_machine_state()
    elif not state_stopping and not state_starting and stop_state:
        # Ожидание истекло без оплаты → STOPPING + глухое окно «КОНЕЦ»
        begin_stopping()
    elif not buttons_state and playpause_state and not stop_state:
        # Игра: в т.ч. продолжение с пульта Arduino (реле → PLAYING).
        # Пульт НЕ идёт на RF_* Pi (там OUTPUT); видим только R_*.
        if state_waiting or not state_playing:
            set_machine_state("PLAYING", playing=True)
    elif state_waiting:
        pass  # ждём PLAYING / STOP
    elif state_playing and buttons_state and playpause_state:
        # PLAYING → WAITING: наша пауза или окно waited после конца времени на Arduino
        our_pause = (time.monotonic() - rf_playpause_sent_at) < RF_PLAY_GRACE_S
        set_machine_state("WAITING", waiting=True)
        state_waiter = not our_pause
    elif buttons_state and playpause_state:
        set_machine_state("WAITING", waiting=True)
        state_waiter = True


def update_state_radio_watchdog():
    """Режим Raspberry: предупреждение, если все RF-входы странно HIGH."""
    global relay_disconnected_warned
    if h is None or not gpio_pins_available.get('RF_INCREASE') or not gpio_pins_available.get('RF_PLAYPAUSE') or not gpio_pins_available.get('RF_STOP'):
        return
    try:
        if lgpio.gpio_read(h, RF_INCREASE) and lgpio.gpio_read(h, RF_PLAYPAUSE) and lgpio.gpio_read(h, RF_STOP):
            if not relay_disconnected_warned:
                log("WARNING: Radio signals look invalid (all HIGH)")
                relay_disconnected_warned = True
            return
        relay_disconnected_warned = False
    except Exception as e:
        log(f"WARNING: Error reading GPIO: {e}")


def updateState():
    if is_arduino_mode():
        update_state_from_relays()
    else:
        update_state_radio_watchdog()


# Главная функция
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("gameStart", "gameStop"):
        raise SystemExit(0)
    try:
        loop()
    except KeyboardInterrupt:
        finish_timer_line()
        log("Timer received KeyboardInterrupt")
    except Exception as e:
        log(f"Timer error: {e}")
    finally:
        teardown_gpio()
        log("Timer shutdown complete")
