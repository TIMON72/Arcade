# Arcade Timer

Таймер аренды «Аркада» для **Batocera на Raspberry Pi 5**.  
Исходник: [Arcade_Timer_Arduino](https://github.com/TIMON72/Arcade_Timer_Arduino).

Сервисы Batocera: `main` · `timer` · `server` · `tvon`.

| Путь на автомате | Назначение |
|------------------|------------|
| `/userdata/system/Arcade` | checkout (исходники; источник для deploy) |
| `/userdata/system/scripts` | рабочий runtime (+ `.venv`, `wheels/`) |
| `/userdata/system/services` | скрипты автозапуска Batocera |
| `/boot/cmdline.txt` | параметры ядра (раздел **BATOCERA**, не SHARE) |

---

## С нуля: что выбрать

| Ситуация | Способ |
|----------|--------|
| Автомат уже в сети, SSH работает | **A. Заливка с ПК по SSH** |
| Свежий SSD / нет сети / серийная прошивка | **B. Offline: Mount + Copy** |
| Только ZIP с GitHub, без Windows-скриптов | **C. На самом автомате (`install`)** |

Репозиторий на Windows — источник правды. Сначала `deploy/machines.example.json` → `deploy/machines.json` (в git не коммитится).

```json
{
  "user": "root",
  "password": "linux",
  "remotePath": "/userdata/system/Arcade",
  "machines": {
    "zero": "192.168.1.72",
    "epsilon2": "192.168.1.66"
  }
}
```

Имя в командах — ключ из `machines` или IP. Пароль: `machines.json` или `ARCADE_SSH_PASSWORD`.

---

## A. С нуля по SSH (рекомендуется для отладки)

Batocera уже загружена, пинг/SSH ок.

```powershell
# 1) полный снос старого Arcade (если был)
.\deploy\clean.ps1 zero -Runtime -Venv

# 2) первая заливка: wheels + vendor + on-device deploy + старт сервисов
.\deploy\deploy.ps1 zero -Full -Restart
```

Что делает `-Full -Restart`:
- кладёт дерево в `/userdata/system/Arcade`
- на устройстве гоняет `main.py deploy` → `scripts/`, `services/`, `batocera.conf`
- создаёт `.venv` и ставит luma из `wheels/` (офлайн)
- дописывает в `/boot/cmdline.txt` защиту Pi5+NVMe (см. ниже)
- `batocera-services restart main timer server tvon`

Дальше, когда правите код с ПК, — раздел **Обычная работа с ПК**.

Чистый переустанов без смены железа:

```powershell
.\deploy\clean.ps1 zero -Runtime -Venv
.\deploy\deploy.ps1 zero -Full -Restart -NoLogs
```

Проверка после бута / power-cycle:

```bash
grep -E 'pcie_aspm=off|nvme_core.default_ps_max_latency' /proc/cmdline
ls -la /userdata/system/scripts/.venv/bin/python   # размер ≥ 64, не 0
batocera-services list | grep -E 'main|timer|server|tvon'
```

---

## B. С нуля offline (SSD на ПК)

Нужны: WSL (Ubuntu), админ-права для `wsl --mount`.

### 1. Собрать бандл `system\`

```powershell
.\deploy\deploy.ps1 -Copy C:\Temp\Batocera\system
```

Получится:

```text
system/
  Arcade/              checkout (+ wheels/vendor)
  scripts/             main timer server tvon + wheels
  scripts/.venv/       если есть vendor/venv/aarch64.tar.gz
  configs/
  services/            main timer server tvon (LF, +x после FixPerms)
  batocera.conf        system.services=main timer server tvon
  .arcade-deployed
```

### 2. Смонтировать SSD и вставить `system\`

```powershell
.\deploy\mount.ps1 -Mount
# откроется SHARE: \\wsl.localhost\Ubuntu\mnt\wsl\batocera-share
# скопировать содержимое C:\Temp\Batocera\system  →  …\batocera-share\system\
.\deploy\mount.ps1 -FixPerms          # после paste: права + +x у services
.\deploy\mount.ps1 N -Unmount         # N = номер диска из mount
```

`mount.ps1 -Mount` также сам правит **boot**-раздел:  
`/mnt/wsl/batocera-boot/cmdline.txt` ← NVMe-токены (ASPM off).

Не путать разделы:

| WSL / Explorer | Batocera | Что там |
|----------------|----------|---------|
| `batocera-share` | `/userdata` (SHARE) | roms, `system/`, Arcade |
| `batocera-boot` | `/boot` (BATOCERA) | `cmdline.txt`, образ ОС |

### 3. SSD в Pi → питание → проверка

Сервисы стартуют из `batocera.conf`. Если не было prebuilt `.venv`, `main` создаст его на первом буте (тяжелее для NVMe).

Опционально заранее снять готовый venv с живой машины:

```powershell
.\deploy\deploy.ps1 zero -PullVenv
# → vendor/venv/aarch64.tar.gz  (потом -Copy кладёт scripts/.venv сразу)
```

---

## C. С нуля только на автомате (`install`)

Без `deploy.ps1`. Положить проект в `/userdata/system/Arcade`, затем:

```bash
bash /userdata/system/Arcade/install
reboot
```

Из GitHub ZIP:

```bash
cd /userdata/system
unzip -o Arcade-main.zip
rm -rf Arcade
mv Arcade-main Arcade
bash /userdata/system/Arcade/install
reboot
```

Снятие сервисов (папку Arcade оставить):

```bash
bash /userdata/system/Arcade/uninstall
reboot
```

Принудительный deploy без reboot:  
`python3 /userdata/system/Arcade/scripts/main/main.py deploy`

---

## Pi5 + NVMe (cmdline)

На связке Pi5 + NVMe без отключения ASPM возможны зависания (`nvme: controller is down`, SSH мёртв при живом ping).

В `/boot/cmdline.txt` (одна строка, в конец) должно быть:

```text
nvme_core.default_ps_max_latency_us=0 pcie_aspm=off pcie_port_pm=off
```

Ставится автоматически: `mount.ps1 -Mount` и SSH-`deploy.ps1`.  
Через `batocera.conf` / меню Batocera **нельзя** — только boot.

Проверка: `cat /proc/cmdline | tr ' ' '\n' | grep -E 'pcie_aspm|nvme_core'`.

---

## Обычная работа с ПК

`zero` в примерах — имя из `deploy/machines.json` (можно IP).  
Без `-Full` **не** копируются `wheels/` и `vendor/` — для правок Python так и нужно.

`-Update` и `-Restart` сейчас одно и то же: файлы на автомат → `main.py deploy` в runtime → restart `main timer server tvon`.

В конце `deploy.ps1` сам открывает follow логов. Чтобы сразу выйти — добавьте `-NoLogs` к любой команде заливки.

### Поправил код — залить и перезапустить сервисы

Основной ежедневный путь. Сервисы подхватывают новый код.

```powershell
.\deploy\deploy.ps1 zero -Update
```

### Поменялись wheels, vendor или снесён .venv

Тяжёлая заливка (много записи на NVMe). После `clean.ps1 … -Venv` нужна именно она.

```powershell
.\deploy\deploy.ps1 zero -Update -Full
```

### Нужно прогнать health (ребут ×3)

Отдельный скрипт, не флаг `deploy.ps1`. Если автомат в сети — ребут. Дальше: SSH ≤1 мин, бут ≤5 мин (когда timer+server живы — `ADD_5`), 5 мин «alive». Три цикла.

```powershell
.\deploy\test.ps1 zero
```

Сначала залить правки, потом проверить:

```powershell
.\deploy\deploy.ps1 zero -Update -NoLogs
.\deploy\test.ps1 zero
```

### Залить файлы, но не рестартить сервисы

Checkout + runtime на диске обновятся, **процессы продолжат старый код в памяти**. Нужно перед Remote-SSH / F5, когда сервисы уже остановлены.

```powershell
.\deploy\deploy.ps1 zero
```

После отладки: `-Update` / `-Restart`.

### Только папка `/userdata/system/Arcade`, runtime не трогать

Файлы репозитория на автомате, `/userdata/system/scripts` не переписывается. Не сочетается с `-Update`.

```powershell
.\deploy\deploy.ps1 zero -NoDeploy
```

### Снести runtime перед чистой установкой

Останавливает сервисы. `-Runtime` — рабочие `scripts/{main,timer,server,tvon}` и service-файлы. `-Venv` — ещё и `.venv` (потом обязателен `-Update -Full` или `-Full -Restart`).

```powershell
.\deploy\clean.ps1 zero -Runtime
.\deploy\clean.ps1 zero -Runtime -Venv
.\deploy\deploy.ps1 zero -Full -Restart -NoLogs
```

### Смотреть логи

По умолчанию — общий `scripts/logs.log` (последние 5 мин, затем follow, Ctrl+C стоп).  
`-Only` — один service-лог. `-AllServices` — `logs.log` + все `*-service.log`.

```powershell
.\deploy\logs.ps1 zero
.\deploy\logs.ps1 zero -Only tvon
.\deploy\logs.ps1 zero -AllServices
.\deploy\logs.ps1 zero -SinceMinutes 10
```

SSH host keys после смены диска меняются: скрипты сами делают `ssh-keygen -R` и `StrictHostKeyChecking=no`.

---

## Отладка с ПК

На автомате: `/userdata/system/logs/<сервис>-service.log`, `/userdata/system/scripts/logs.log`.

Remote-SSH + debugpy:

1. `.\deploy\deploy.ps1 zero -NoLogs`
2. Cursor → `root@<IP>` → open `/userdata/system/Arcade`
3. F5 → compound **Arcade**

После отладки: `.\deploy\deploy.ps1 zero -Update -NoLogs`.

Обновление без ПК (если на автомате есть git):

```bash
cd /userdata/system/Arcade && git pull && reboot
```

---

## Сервисы

| Сервис | Роль |
|--------|------|
| **main** | deploy, venv, sync Arcade → system |
| **timer** | GPIO, таймер, матрица; Unix-сокет |
| **server** | HTTP `:5000` → `modules/` |
| **tvon** | HDMI-CEC: ТВ → картинка → звук |

Команда таймеру:  
`http://<IP>:5000/test?action=INCREASE|PLAYPAUSE|STOP|ADD_<INT>`

```bash
batocera-services status main
batocera-services status timer
batocera-services status server
batocera-services status tvon
```

### TVON (кратко)

Этапы: TV power → HDMI/claim → picture → audio → post-check → при необходимости ES kick.  
Порт HDMI `auto` (A-1 или A-2). Конфиг: `scripts/tvon/config.toml`.

Если после cold boot долго нет звука — часто HDMI sink ещё `auto_null`; подождать или зафиксировать устройство в меню звука Batocera.

---

## Конфиг модулей

Править в Arcade, потом `deploy.ps1` / reboot:

- `scripts/main/config.toml` — SSH  
- `scripts/timer/config.toml` — режим, GPIO, матрица, `cmd_socket`  
- `scripts/server/config.toml` — `port`, `timer_socket`  
- `scripts/tvon/config.toml` — CEC / этапы  

`cmd_socket` и `timer_socket` должны совпадать (по умолчанию `/var/run/arcade-timer.sock`).  
`timer_mode`: `Raspberry` | `Arduino`.

---

## Железо (GPIO BCM)

![GPIO BCM](pins.png)

| Что | Пины |
|-----|------|
| RF + / play / stop | 5 / 6 / 13 |
| Реле buttons / play / stop | 17 / 27 / 22 |
| Терминал (сухой контакт) | 26 (+ GND) |
| MAX7219 DIN / CLK / CS | 10 / 11 / 8 |

Общая земля Pi и БП матрицы обязательна. БП Pi5: ориентир **5 V / 5 A**.

---

## Офлайн-зависимости в репо

| Путь | Зачем |
|------|--------|
| `wheels/` | pip offline (luma) |
| `vendor/git/` | портативный git |
| `vendor/vscode/` | VSIX для Cursor |
| `vendor/venv/aarch64.tar.gz` | готовый `.venv` для `-Copy` (после `-PullVenv`) |

Не в git: `.venv/`, `logs.log`, `.arcade-deployed`, `deploy/machines.json`.
