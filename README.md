# Arcade Timer

Таймер аренды «Аркада» для Batocera на Raspberry Pi 5.  
Исходник: [Arcade_Timer_Arduino](https://github.com/TIMON72/Arcade_Timer_Arduino).

## Установка (после Batocera)

По SSH (или локальный shell): положить проект в `/userdata/system/Arcade`, затем `install` + reboot.

### Из архива GitHub (`Arcade-main.zip`)

Скачать ZIP с GitHub, закинуть на автомат (scp/USB) в `/userdata/system/`, затем:

```bash
cd /userdata/system
unzip -o Arcade-main.zip
rm -rf Arcade
mv Arcade-main Arcade
bash /userdata/system/Arcade/install
reboot
```

GitHub распаковывает в `Arcade-main/` — его нужно переименовать в `Arcade`.

### Уже лежит в `/userdata/system/Arcade`

```bash
bash /userdata/system/Arcade/install
reboot
```

(`bash`, не `./install` — после unzip/`git clone` у файла может не быть `+x`.)

Готово. Сервисы: `main`, `timer`, `server`, `tvon`.

### Снятие (оставить папку Arcade)

```bash
bash /userdata/system/Arcade/uninstall
reboot
```

Убирает сервисы, `system.services=…` и `/userdata/system/scripts`.  
Каталог `/userdata/system/Arcade` (исходники) не удаляется.

| Путь | Роль |
|------|------|
| `/userdata/system/Arcade` | исходники |
| `/userdata/system/scripts` | рабочий код |
| `/userdata/system/services` | скрипты сервисов Batocera |

Принудительно без reboot: `python3 /userdata/system/Arcade/scripts/main/main.py deploy`

---

## Разработка с ПК (Windows)

Репозиторий на ПК — источник правды. На автомате только прогон и отладка.

### 1. Список автоматов

```text
deploy/machines.example.json  →  deploy/machines.json
```

`machines.json` в git не коммитится. Пример:

```json
{
  "user": "root",
  "password": "linux",
  "remotePath": "/userdata/system/Arcade",
  "machines": {
    "zero": "192.168.1.120",
    "epsilon2": "192.168.1.66"
  }
}
```

Имя машины в командах — ключ из `machines` (или сразу IP).
Пароль также можно задать через `ARCADE_SSH_PASSWORD`. Удобнее один раз положить SSH pubkey в `authorized_keys` на автомате.

Host keys генерит сама Batocera (после смены NVMe fingerprint меняется).  
`deploy.ps1` / `logs.ps1` перед коннектом делают `ssh-keygen -R <IP>` и ходят с `StrictHostKeyChecking=no` + пустым known_hosts.  
В `~/.ssh/config` для `192.168.1.*` лучше `StrictHostKeyChecking no` (не `accept-new` — после смены диска `accept-new` ломает вход).  
MobaXterm хранит ключи отдельно — один раз Accept на новый fingerprint.

### 2. Заливка кода

Из корня репо:

```powershell
.\deploy\deploy.ps1 zero                 # sync + deploy + сразу logs.log
.\deploy\deploy.ps1 zero -Restart        # + restart сервисов, затем логи
.\deploy\deploy.ps1 zero -Full -Restart  # + wheels/ и vendor/ (первый раз / офлайн)
.\deploy\deploy.ps1 zero -NoDeploy       # только файлы в /userdata/system/Arcade
.\deploy\deploy.ps1 zero -NoLogs         # без автоподключения логов
```

Без `-Full` не копируются тяжёлые `wheels/` и `vendor/`.

Cursor: **Tasks: Run Task** → `Sync to machine` / `Sync to machine + restart services`.

### 3. Логи

```powershell
.\deploy\logs.ps1 zero                 # /userdata/system/scripts/logs.log за последние 5 мин + follow
.\deploy\logs.ps1 zero -SinceMinutes 10
.\deploy\logs.ps1 zero -AllServices    # + per-service логи
.\deploy\logs.ps1 zero -Only timer
```

Ctrl+C — стоп. После ребута автомата сессия сама переподключается (ServerAlive + retry). Task: **Remote logs**.
На автомате те же файлы: `/userdata/system/logs/<сервис>-service.log`, `/userdata/system/scripts/logs.log`.

### 4. Отладка (Remote-SSH + debugpy)

1. `.\deploy\deploy.ps1 zero`
2. Cursor **Remote-SSH** → `root@<IP>`
3. Open Folder → `/userdata/system/Arcade`
4. F5 → compound **Arcade** (Main + Timer + Server)

Логи дебага — в **Debug Console**. Сессию переключай в Call Stack: Main / Timer / Server. Отдельно те же конфиги + Tvon.

PreLaunch tasks останавливают Batocera-сервисы, чтобы не было конфликта GPIO/сокетов. После отладки вернуть сервисы: `.\deploy\deploy.ps1 zero -Restart`.

### Обновление без ПК (на автомате)

```bash
cd /userdata/system/Arcade && git pull && reboot
```

`main` при старте сам задеплоит изменения из Arcade в `scripts` / `services`.

---

## Сервисы

| Сервис | Что делает |
|--------|------------|
| **main** | deploy, venv, лог; при старте sync Arcade → system |
| **timer** | GPIO, таймер, матрица; Unix-сокет команд |
| **server** | HTTP `:5000`, webhooks → `modules/` |
| **tvon** | HDMI-CEC: ТВ → вход → картинка → звук (по этапам) |

Команда таймеру: `http://<IP>:5000/test?action=INCREASE|PLAYPAUSE|STOP|ADD_<INT>`  
→ `server/modules/timer.py` → сокет → `timer.action()`.

## TVON (HDMI-CEC)

Сервис `tvon` при старте Batocera поднимает картинку на ТВ по этапам с recovery:

1. **TV power** — CEC `on` / опрос `pow`
2. **HDMI + claim** — ждёт DRM `connected`, claim Active Source, держит длинную CEC-сессию (keeper), иначе часть ТВ сбрасывает вход в `unknown (-1)`
3. **Picture** — `batocera-resolution setOutput` на живой выход
4. **Audio** — soft `set-profile`/`set`; hard `S06audio` только если нет HDMI sink
5. **Post-check** — короткая проверка, что картинка/вход/звук не отвалились
6. **ES kick (по необходимости)** — если этапы ок, но ES стартовал с пустым `settled display list: [ ]` (медленный ТВ), один раз: `setOutput` + `batocera-es-swissknife --restart`. Быстрый ТВ с уже заполненным списком — **не** рестартим

Порт HDMI **не фиксирован**: `hdmi_output = "auto"` берёт `HDMI-A-1` или `HDMI-A-2` (что `connected`). CEC physical address тоже `auto` (`1.0.0.0` / `2.0.0.0`). Кабель можно воткнуть в любой HDMI Pi5; на ТВ нужен вход с CEC.

Конфиг: `scripts/tvon/config.toml` (ключевые флаги: `restart_es_if_empty`, `active_source_keep_sec`, `audio_hard_restart`; безусловный `restart_es` по умолчанию выключен — на Pi5+NVMe опасен).

Логи: `.\deploy\logs.ps1 <машина> -Only tvon` или `grep TVON /userdata/system/scripts/logs.log`.

## Конфиг

У каждого модуля свой файл (править в Arcade, затем `deploy.ps1` / reboot):

- `scripts/main/config.toml` — SSH  
- `scripts/timer/config.toml` — режим, GPIO, терминал, матрица, `cmd_socket`  
- `scripts/server/config.toml` — `port`, `timer_socket`  
- `scripts/tvon/config.toml` — CEC / этапы TVON  

`cmd_socket` и `timer_socket` должны совпадать (по умолчанию `/var/run/arcade-timer.sock`).

`timer_mode`: `Raspberry` (Pi сам) или `Arduino` (Pi имитирует радио).

## Проверка

```bash
batocera-services status main
batocera-services status timer
batocera-services status server
batocera-services status tvon
```

## Железо

![GPIO BCM](pins.png)

| Что | Пины (BCM) |
|-----|------------|
| RF + / play / stop | 5 / 6 / 13 |
| Реле buttons / play / stop | 17 / 27 / 22 |
| Терминал (сухой контакт) | 26 (+ GND) |
| MAX7219 DIN / CLK / CS | 10 / 11 / 8 |

Общая земля Pi и БП матрицы обязательна.

## Заметки

Офлайн-зависимости в репо: `wheels/`, `vendor/git/`, `vendor/vscode/`.  
Не в git: `.venv/`, `logs.log`, `.arcade-deployed`, `deploy/machines.json`.
