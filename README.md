# Arcade Timer

Таймер аренды игрового автомата «Аркада» для **Batocera** на **Raspberry Pi 5**.

Портирован с [Arduino-проекта](https://github.com/TIMON72/Arcade_Timer_Arduino): управление реле кнопок автомата, обратный отсчёт на LED-матрице MAX7219, веб-интерфейс для тестов.

## Возможности

| Событие | Дисплей / действие |
|--------|---------------------|
| Простой (00:00:00) | Бегущая строка из конфига |
| Кнопка «+» | Время `MM:SS` или `HH:MM` |
| Старт / возобновление | «ИГРА» + обратный отсчёт |
| Пауза | «ПАУЗА» |
| Идёт отсчёт | Обновление каждую секунду |
| Режим ожидания | `$?SS` (рубль, вопрос, секунды) |
| Стоп | «КОНЕЦ» → пауза → снова бегущая строка |
| Время выставлено, игра не запущена | Автосброс через `time_reset` минут |

Веб-сервер (`aiohttp`) на порту **5000**: `GET /test?action=...` для удалённых команд.

## Структура репозитория

```
Arcade/
├── batocera.conf          # настройки Batocera (system.services=main)
├── config_timer.toml      # конфиг таймера, GPIO и матрицы
├── requirements.txt       # только для vendor-wheels (не deploy)
├── wheels/                # офлайн-пакеты pip (aarch64, Python 3.12)
├── configs/               # пользовательские конфиги Batocera
├── services/
│   └── main               # сервис Batocera (start/stop/status)
└── scripts/
    ├── main.py            # точка входа, deploy, venv
    ├── timer.py           # логика таймера и GPIO
    ├── server.py          # веб-сервер
    └── modules/
        ├── matrix.py      # драйвер MAX7219 (luma, bitbang SPI)
        ├── matrix_glyphs.py
        ├── matrix_font5x8.py
        └── lgpio_gpio.py
```

### После развёртывания на Batocera

```
/userdata/system/
├── .arcade-deployed       # маркер первого deploy (не в git)
├── batocera.conf
├── configs/
├── services/main
└── scripts/
    ├── main.py
    ├── modules/
    ├── wheels/            # копия из репозитория
    ├── config_timer.toml
    └── venv/              # создаётся при первом запуске
```

Репозиторий может лежать **где угодно** (флешка, `/userdata/system/Arcade`, и т.д.). Рабочие пути Batocera фиксированы: `/userdata/system/scripts` и `/userdata/system/services`.

## Требования

| Компонент | Где |
|-----------|-----|
| Batocera, Python 3.12 | система |
| `lgpio`, `aiohttp` | системный Python Batocera |
| `luma.led_matrix` | venv (ставится офлайн из `wheels/`) |

Интернет на консоли **не обязателен** — wheel-файлы включены в репозиторий.

## Первый запуск на Batocera

1. Скопируйте проект на консоль (git clone, SCP, флешка).
2. Запустите из каталога проекта:

```bash
python3 scripts/main.py
```

При первом запуске автоматически:

- развернёт `configs/`, `services/`, `scripts/`, `wheels/`, `config_timer.toml` в `/userdata/system/`;
- перезапишет `batocera.conf` версией из проекта;
- создаст маркер `/userdata/system/.arcade-deployed`;
- создаст `venv` в `scripts/` и установит `luma` из локальных wheels.

3. Перезагрузите Batocera или запустите сервис:

```bash
/userdata/system/services/main start
/userdata/system/services/main status
```

Лог сервиса: `/userdata/system/logs/main-service.log`

## Команды main.py

```bash
python3 scripts/main.py              # deploy (если нужно) + запуск
python3 scripts/main.py deploy       # принудительное обновление файлов на Batocera
python3 scripts/main.py vendor-wheels   # скачать wheels (нужен интернет и pip)
```

### Повторный deploy

```bash
python3 scripts/main.py deploy
```

Или удалите маркер и перезапустите:

```bash
rm /userdata/system/.arcade-deployed
python3 scripts/main.py
```

## Конфигурация

Файл `config_timer.toml` в корне репозитория (после deploy — в `/userdata/system/scripts/`).

```toml
[timer]
time_step = 5      # шаг «+», минуты
time_wait = 60     # пауза после окончания, секунды
time_reset = 5       # автосброс без старта, минуты

[gpio]
rf_increase = 5
rf_playpause = 6
rf_stop = 13
r_buttons = 17
r_playpause = 27
r_stop = 22
relay_active_low = true

[matrix]
enabled = true
brightness = 7
scroll_speed = 7
text_display = "АРЕНДА: т. +79233549295"
din = 10
clk = 11
cs = 8
cascaded = 4
block_orientation = 90
blocks_reverse = true
rotate = 2
test_on_start = true
```

## Аппаратура

- **Raspberry Pi 5** с Batocera
- Реле автомата (GPIO 17, 27, 22)
- RF-кнопки пульта (GPIO 5, 6, 13)
- MAX7219: 4 модуля 8×8 в ряд, bitbang SPI (DIN=10, CLK=11, CS=8)
- Общая земля Pi и блока питания матрицы обязательна

## Разработка

```bash
# venv (создаётся автоматически при первом запуске main.py)
python3 scripts/main.py

# Обновить wheels на машине с интернетом
python3 scripts/main.py vendor-wheels
git add wheels/
```

В VS Code: конфигурации запуска в `.vscode/launch.json` (`Main`, `Server`, `Timer`, `All`).

### Зависимости

- **В git:** `wheels/` (офлайн-установка на консоли)
- **Не в git:** `venv/`, `logs.log`, `.arcade-deployed`
- **`requirements.txt`** — только для `vendor-wheels`, на Batocera не копируется

## Лицензия и авторство

Основано на проекте [Arcade_Timer_Arduino](https://github.com/TIMON72/Arcade_Timer_Arduino) (Радионов Тимофей).
