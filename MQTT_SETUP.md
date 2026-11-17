# Подключение аркады к Вендоматике по MQTT

Инструкция для Batocera на Raspberry Pi. Подходит и для первого автомата, и для следующих.

Что публикует Raspberry:

- online/offline в `tcn/{machineId}/status` каждые 30 секунд;
- список игр из `/userdata/roms/*/gamelist.xml`;
- `playcount` — число запусков;
- `gametime` — суммарные секунды игры, в отчёте показываются минуты;
- `lastplayed` — последняя дата запуска.

Изменения каталога отправляются при старте и при проверке раз в 60 секунд. Отчёт находится в PWA: **Автоматы → нужная аркада → «Игры на автомате»**.

## Важно

- `machineId` = серийный номер аркады в Вендоматике, он же серийный номер куба aQsi.
- У каждого автомата свои `machineId`, логин и пароль MQTT. Не копировать их с другого автомата.
- Реальный конфиг на Raspberry: `/userdata/system/mqtt.json`.
- Пример без секретов в репозитории: `scripts/server/mqtt.example.json`.
- Локальная рабочая копия `scripts/server/mqtt.json` добавлена в `.gitignore` и не попадёт в Git.
- Конфиг `/userdata/system/mqtt.json` находится вне репозитория и обычный deploy его не затирает.

Текущие автоматы:

| Автомат | IP | `machineId` |
|---|---|---|
| Красной гвардии 24 (аркада) | `192.168.3.156` | `2010603087004659` |
| Красной гвардии (Харатс) | `192.168.3.205` | `2010305589004590` |

SSH на Batocera: пользователь `root`, пароль `linux`.

---

## 1. Обновление уже подключённого автомата

Для обычного обновления кода на ПК с Windows:

```powershell
cd C:\Repo\Arcade

# На этом ПК обязательно сначала выбрать системный tar, а не tar из Git.
$env:PATH = "C:\Windows\System32;" + $env:PATH

# Обновить первый автомат, без повторной заливки тяжёлых vendor/wheels.
.\deploy\deploy.ps1 192.168.3.156 -Auth linux -Update -NoLogs
```

Скрипт сам:

- заливает изменённые файлы в `/userdata/system/Arcade`;
- обновляет runtime в `/userdata/system/scripts`;
- перезапускает `main`, `timer`, `server`, `tvon`;
- сохраняет `/userdata/system/mqtt.json`.

После изменения `requirements.txt` или файлов в `wheels/` один раз нужен полный deploy:

```powershell
.\deploy\deploy.ps1 192.168.3.156 -Auth linux -Update -Full -NoLogs
```

Проверка:

```powershell
ssh root@192.168.3.156
batocera-services status server
tail -n 30 /userdata/system/logs/server-service.log
```

Ожидаемые строки:

```text
MQTT: connected
MQTT games: published N game(s)
```

---

## 2. Первичное подключение нового автомата

Пример ниже — для второго автомата `192.168.3.205` и аркады `2010305589004590`.

### 2.1. Убедиться, что аркада есть в Вендоматике

Эта аркада уже заведена на сервере. Если добавляете другую:

1. Откройте PWA под админом или суперадмином.
2. Нажмите **Добавить автомат → Аркада**.
3. Укажите адрес и серийный номер куба aQsi.
4. Сохраните автомат. Его ID и будет нужен дальше как `machineId`.

Продажи продолжают приходить из куба. Raspberry добавляет свой online и каталог игр; после первого MQTT-статуса Raspberry её online/offline имеет приоритет над опросом куба.

### 2.2. Залить проект и зависимости на новую Raspberry

С ПК:

```powershell
cd C:\Repo\Arcade
$env:PATH = "C:\Windows\System32;" + $env:PATH

# Полный deploy обязателен: так на автомат попадут paho-mqtt и остальные wheels.
.\deploy\deploy.ps1 192.168.3.205 -Auth linux -Full -Restart -NoLogs
```

Если Batocera чистая и ещё не настроена, сначала проверьте, что по SSH работают:

```powershell
ping 192.168.3.205
ssh root@192.168.3.205
```

После deploy должны запуститься четыре сервиса:

```powershell
ssh root@192.168.3.205 "batocera-services status main; batocera-services status timer; batocera-services status server; batocera-services status tvon"
```

### 2.3. Получить личную MQTT-учётку именно этого автомата

На сервере Вендоматики:

```bash
cd /opt/vend/pwavending
docker compose exec -T gateway /app/mqttcredentials -machine 2010305589004590
```

Если учётки ещё не было, команда создаст её. Повторный запуск покажет уже существующую.

Ответ будет JSON примерно такого вида:

```json
{
  "enabled": true,
  "broker": "tcp://MQTT_SERVER:1883",
  "username": "tcn-2010305589004590",
  "password": "СГЕНЕРИРОВАННЫЙ_ПАРОЛЬ",
  "machineId": "2010305589004590",
  "topicPrefix": "tcn/2010305589004590"
}
```

Это секретные данные. Не отправляйте их в Git и не оставляйте в общих инструкциях.

### 2.4. Создать локальный конфиг из примера

На ПК:

```powershell
cd C:\Repo\Arcade
Copy-Item .\scripts\server\mqtt.example.json .\scripts\server\mqtt.json
notepad .\scripts\server\mqtt.json
```

Перенесите значения из ответа `mqttcredentials`:

```json
{
  "enabled": true,
  "broker": "tcp://MQTT_SERVER:1883",
  "username": "tcn-2010305589004590",
  "password": "СГЕНЕРИРОВАННЫЙ_ПАРОЛЬ",
  "machineId": "2010305589004590",
  "topicPrefix": "tcn/2010305589004590",
  "gameSystems": ["nes", "dreamcast"],
  "heartbeatSec": 30,
  "gamesRefreshSec": 60
}
```

Проверьте особенно:

- `machineId` именно этой аркады;
- `topicPrefix` содержит тот же ID;
- логин и пароль именно этой аркады;
- `broker` — внешний адрес MQTT-сервера, доступный с Raspberry, не `localhost`.

Файл `scripts/server/mqtt.json` находится в `.gitignore`, поэтому останется только на вашем ПК.

### 2.5. Записать конфиг и перезапустить сервер

С ПК:

```powershell
scp .\scripts\server\mqtt.json root@192.168.3.205:/userdata/system/mqtt.json
ssh root@192.168.3.205 "chmod 600 /userdata/system/mqtt.json && sync && batocera-services restart server"
```

Файл на Raspberry должен получиться таким:

```text
/userdata/system/mqtt.json
```

Дальше этот файл можно редактировать только при смене брокера или учётки. Обычные обновления проекта его не перезаписывают.

### 2.6. Проверить подключение

```powershell
ssh root@192.168.3.205 "tail -n 40 /userdata/system/logs/server-service.log"
```

Ожидается:

```text
MQTT: connecting to … as 2010305589004590
MQTT: connected
MQTT games: published N game(s)
```

Дополнительно:

```powershell
ssh root@192.168.3.205 "batocera-services status server"
curl http://192.168.3.205:5000/
```

Затем откройте отчёт этой аркады в PWA. Должны появиться:

- online от Raspberry;
- список установленных игр;
- число запусков;
- минуты;
- последняя дата игры.

---

## 3. Добавление или изменение игр

1. Залейте ROM в нужную папку внутри `/userdata/roms/`.
2. В EmulationStation обновите список игр.
3. Убедитесь, что игра есть в соответствующем `gamelist.xml`.
4. Подождите до 60 секунд либо перезапустите сервис:

```powershell
ssh root@192.168.3.205 "batocera-services restart server"
```

Счётчики `playcount` и `gametime` берутся из штатных данных EmulationStation. Полной истории каждой сессии Batocera сейчас нет: `lastplayed` хранит только последний запуск.

Из отчёта аркады можно загрузить или удалить ROM. Загрузка идёт частями по 4 МБ с проверкой SHA-256. Удаление перемещает файл в `/userdata/system/arcade-trash/`, поэтому его можно вернуть вручную.

---

## 4. Если MQTT не подключается

Проверить конфиг без вывода пароля на экран:

```powershell
ssh root@192.168.3.205 "chmod 600 /userdata/system/mqtt.json; batocera-services restart server; tail -n 40 /userdata/system/logs/server-service.log"
```

Типовые причины:

| Симптом | Что проверить |
|---|---|
| `machineId is required` | пустой или неверный `machineId` |
| `broker must use tcp://host:port` | адрес должен начинаться с `tcp://` |
| `connection refused` | неверный логин/пароль или учётка другого автомата |
| долго нет `connected` | недоступен порт `1883`, Wi-Fi или внешний адрес сервера |
| `gamelist.xml` нулевой после reboot | агент сам восстановит его из Batocera `datainit` и перечитает; если эталона нет — обновите список в EmulationStation |
| online есть, игр нет | нет `gamelist.xml`, ошибка XML, игра ещё не попала в список EmulationStation |
| online от Cube не меняется | после появления Raspberry её статус имеет приоритет; проверяйте heartbeat от Raspberry |
| `paho-mqtt is not installed` | нужен один полный deploy с `-Full` |

---

## 5. Короткий чеклист второго автомата

- [ ] `192.168.3.205` доступен по SSH.
- [ ] Выполнен `deploy.ps1 192.168.3.205 -Auth linux -Full -Restart -NoLogs`.
- [ ] На сервере получена учётка через `mqttcredentials -machine 2010305589004590`.
- [ ] В `scripts/server/mqtt.json` внесены значения именно второй аркады.
- [ ] Конфиг скопирован в `/userdata/system/mqtt.json`, права `600`.
- [ ] `server` перезапущен, в логе `MQTT: connected`.
- [ ] В отчёте аркады видны online, игры, запуски и минуты.
- [ ] Файл с паролем не закоммичен в Git.
