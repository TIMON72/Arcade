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

Веб-сервер (`aiohttp`): порт из `[server]` в конфиге (по умолчанию **5000**): `GET /test?action=...` для удалённых команд.

## Структура репозитория

```
Arcade/
├── batocera.conf          # настройки Batocera (system.services=main)
├── pins.png               # схема распиновки GPIO (BCM)
├── requirements.txt       # только для vendor-wheels (не deploy)
├── wheels/                # офлайн-пакеты pip (aarch64, Python 3.12)
├── vendor/git/            # портативный git (tar.xz) для офлайн-установки
├── vendor/vscode/         # VSIX Python/debugpy для Cursor SSH
├── configs/               # пользовательские конфиги Batocera
├── services/
│   └── main               # сервис Batocera (start/stop/status)
└── scripts/
    ├── main.py            # точка входа, deploy, venv
    ├── config_main.toml   # конфиг сервера, таймера, GPIO и матрицы
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
├── git/                   # портативный git (из vendor/git при deploy)
├── services/main
└── scripts/
    ├── main.py
    ├── modules/
    ├── wheels/            # копия из репозитория
    ├── config_main.toml
    └── .venv/             # создаётся при первом запуске
```

Репозиторий может лежать **где угодно** (флешка, `/userdata/system/Arcade`, и т.д.). Рабочие пути Batocera фиксированы: `/userdata/system/scripts` и `/userdata/system/services`.

## Требования

| Компонент | Где |
|-----------|-----|
| Batocera, Python 3.12 | система |
| `lgpio`, `aiohttp` | системный Python Batocera |
| `luma.led_matrix` | `.venv` (ставится офлайн из `wheels/`) |
| `git` | `/userdata/system/git` + симлинки в `/usr/bin` (из `vendor/git/` при deploy) |
| Cursor: Python / debugpy | remote extensions (из `vendor/vscode/*.vsix` при deploy) |

Интернет на консоли **не обязателен** — wheel-файлы, tarball git и VSIX включены в репозиторий.

## Развёртывание на автоматах

Два независимых каталога:

| Путь | Назначение |
|------|------------|
| `/userdata/system/Arcade` | клон git (исходники, `pull` / разработка) |
| `/userdata/system/scripts` (+ `services/`, …) | **рабочая** копия, из которой крутится сервис |

`deploy` копирует **односторонне**: `Arcade` → `/userdata/system/`. Правки только в `scripts/` на git не влияют. Коммиты и `push` — **только с машины разработки**.

Репозиторий: https://github.com/TIMON72/Arcade.git

### Машина разработки (этот автомат)

Код правите в `Arcade`, проверяете через deploy:

```bash
cd /userdata/system/Arcade
# … правки, commit …
git push origin main

python3 scripts/main.py deploy
batocera-services restart main
```

### Другой автомат — первая установка

Нужны сеть и доступ к GitHub (для private — токен/ключ).

```bash
cd /userdata/system
git clone https://github.com/TIMON72/Arcade.git Arcade
cd Arcade
python3 scripts/main.py
batocera-services enable main
batocera-services restart main
```

Первый `main.py` (пока нет `/userdata/system/.arcade-deployed`) сам сделает deploy и поднимет таймер:

1. Скопирует `configs/`, `services/`, `scripts/` (включая `config_main.toml`), `wheels/` в `/userdata/system/`
2. Перезапишет `batocera.conf`
3. Установит портативный `git` из `vendor/git/` и симлинки в `/usr/bin`
4. Установит расширения Cursor Python/debugpy (из `vendor/vscode/` или marketplace)
5. Создаст маркер `.arcade-deployed`
6. Создаст `.venv` в `/userdata/system/scripts/` и поставит `luma` из wheels
7. Перезапустится из `/userdata/system/scripts/main.py`

После reboot симлинки `/usr/bin/git*` поднимает сервис `main` (корневая ФС Batocera сбрасывается).

Опционально запретить случайный `push` с продакшен-автомата:

```bash
cd /userdata/system/Arcade
git remote set-url --push origin DISABLED
```

`git pull` при этом продолжит работать.

Локальные отличия (телефон в бегущей строке, `timer_mode` и т.п.) правьте в **рабочем** конфиге:

```bash
nano /userdata/system/scripts/config_main.toml
batocera-services restart main
```

`deploy` снова перезапишет этот файл из репозитория — либо держите машинно-специфичное в git (отдельные значения/ветки), либо поправляйте после каждого обновления.

### Другой автомат — обновление через pull

На машине разработки: `git push origin main`.  
На втором автомате:

```bash
cd /userdata/system/Arcade
git pull origin main
python3 scripts/main.py deploy
batocera-services restart main
```

Повторный `python3 scripts/main.py` из `Arcade/` **не** обновляет `/userdata/system/` — только `deploy` (или удаление маркера `.arcade-deployed` и снова `main.py`).

### Сравнение команд

| Команда | Deploy | Запуск таймера | Когда использовать |
|---------|--------|----------------|-------------------|
| `python3 scripts/main.py` | только первый раз | да | первая установка |
| `python3 scripts/main.py deploy` | всегда | нет | после `git pull` / правок |
| `batocera-services restart main` | нет | через сервис | обычный перезапуск |

### Автозапуск при загрузке

В `batocera.conf` проекта: `system.services=main` — после deploy попадает в `/userdata/system/batocera.conf`. Либо `reboot`, либо `batocera-services enable main`.

### Логи и проверка

```bash
batocera-services status main
```

| Лог | Путь |
|-----|------|
| Сервис (stdout/stderr) | `/userdata/system/logs/main-service.log` |
| Приложение | `/userdata/system/scripts/logs.log` |

В логе: `MAIN service STARTED`, `'server.py' started`. Веб: `http://<IP-консоли>:5000/`

## Команды main.py

```bash
python3 scripts/main.py              # авто-deploy (если первый раз) + запуск
python3 scripts/main.py deploy       # принудительный deploy (git + IDE extensions)
python3 scripts/main.py install-git  # только установить git из vendor/git
python3 scripts/main.py install-extensions  # только Cursor Python/debugpy
python3 scripts/main.py vendor-wheels       # скачать wheels (нужен интернет и pip)
python3 scripts/main.py vendor-git          # скачать tarball git в vendor/git/
python3 scripts/main.py vendor-extensions   # скачать VSIX в vendor/vscode/
```

## Конфигурация

Файл `scripts/config_main.toml` (после deploy — `/userdata/system/scripts/config_main.toml`).

```toml
[server]
port = 5000

[timer]
timer_mode = "Raspberry"  # или "Arduino" — Pi имитирует радио на rf_* → Arduino
time_step = 5      # шаг «+», минуты
time_wait = 60     # пауза после окончания, секунды
time_reset = 5       # автосброс без старта, минуты

[gpio]
# Raspberry: rf_*=вход пульта, r_*=выход реле
# Arduino:  rf_*=выход «радио», r_*=вход состояния с Arduino
rf_increase = 5
rf_playpause = 6
rf_stop = 13
r_buttons = 17
r_playpause = 27
r_stop = 22
relay_active_low = true

[terminal]
enabled = true
pin = 26             # BCM / физ. 37; GND = физ. 39
active_high = false  # покой=1 (pull-up), импульс=0 (замыкание на GND)
debounce_ms = 0
start_delay_ms = 2000

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

![Распиновка GPIO (BCM)](pins.png)

- **Raspberry Pi 5** с Batocera
- Режим `timer_mode`: **Raspberry** (Pi управляет реле/таймером/матрицей) или **Arduino** (Pi шлёт импульсы на rf_* 5/6/13, читает состояние с r_* 17/27/22)
- Реле автомата (GPIO 17, 27, 22) — выходы в режиме Raspberry
- RF-кнопки пульта (GPIO 5, 6, 13) — входы в режиме Raspberry; в режиме Arduino — выходы «имитация радио»
- Сухой контакт терминала (GPIO 26 / физ. 37 + GND / физ. 39): замыкание на землю = импульс; каждый импульс = `+time_step`, после `start_delay_ms` — автозапуск
- MAX7219: 4 модуля 8×8 в ряд, bitbang SPI (DIN=10, CLK=11, CS=8) — в режиме Raspberry
- Общая земля Pi и блока питания матрицы обязательна

## Разработка

```bash
# .venv (создаётся автоматически при первом запуске main.py)
python3 scripts/main.py

# Обновить wheels на машине с интернетом
python3 scripts/main.py vendor-wheels
git add wheels/
```

В VS Code: конфигурации запуска в `.vscode/launch.json` (`Main`, `Server`, `Timer`, `All`).

### Зависимости

- **В git:** `wheels/`, `vendor/git/*.tar.xz`, `vendor/vscode/*.vsix` (офлайн-установка на консоли)
- **Не в git:** `.venv/`, `venv/`, `logs.log`, `.arcade-deployed`, `/userdata/system/git`
- **`requirements.txt`** — только для `vendor-wheels`, на Batocera не копируется

## Лицензия и авторство

Основано на проекте [Arcade_Timer_Arduino](https://github.com/TIMON72/Arcade_Timer_Arduino) (Радионов Тимофей).
