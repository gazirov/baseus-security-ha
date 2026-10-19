# Baseus Security S1: результаты анализа Android-приложения

> Ниже — исходный статический анализ APK 1.11.0. Позже реализован Python-клиент
> с Android-мостом и проверена реальная съёмка на установленной версии 1.5.0.
> Актуальные результаты: [VALIDATION_RU.md](VALIDATION_RU.md), запуск: [README.md](README.md).

## Статус

Исследован локальный `baseus+Security_1.11.0_APKPure.xapk`:
пакет `com.baseus.security.ipc`, версия 1.11.0, versionCode 52.
Это статический анализ: вход в аккаунт, пробуждение физической камеры и получение
свежего кадра пока не проверялись. Рабочего самостоятельного клиента камеры пока нет.

**Основной вывод:** найден путь пробуждения через XM MQTT и получения изображения
через XM P2P. Есть достаточно конкретных точек входа для прототипа, но одного
подтверждённого HTTP URL для свежего JPEG не обнаружено в исследованной цепочке.

## 1. Важно отличать S1 от S1 Lite

В приложении одновременно присутствуют SDK XM/Xmitech и Tuya/ThingClips.
Само наличие Tuya в APK не означает, что конкретная камера работает через Tuya.

`CurrentDeviceModel.java` задаёт:

- S1: `S0SW00`;
- S1 Lite: `S00033`.

S1 Lite явно включена в `TuyaDeviceProduct.java`, обычная S1 — нет.
Обычная S1 обрабатывается также в XM-ветке приложения. Поэтому для заявленной
S1 основной кандидат — XM; точную модель и прошивку следует сверить с устройством.

## 2. Восстановленная схема

```text
Вход в Baseus с учётом региона
  → токен/параметры аккаунта и региональных сервисов
  → данные устройства и P2P-платформы
  → получение MQTT-реквизитов
  → MQTT WatchDevice (пробуждение)
  → XM P2P-соединение
  → приём и декодирование видео
  → получение bitmap → сохранение JPEG
  → остановка потока и закрытие сессии
```

Это архитектурная последовательность. Точный порядок асинхронных callback,
подключение MQTT и повторные попытки нужно воспроизвести при реализации.

## 3. HTTP и авторизация

В `AccountApi.java` найден:

```text
POST /api/auth/account/login?countryCode=...
```

В `AccountDataApiImpl.java` тело строится из `type: 0`, `account` и `password`.
Пароль перед отправкой обрабатывается `RSAUtils.a(...)`, то есть подставить
обычный пароль прямо в JSON недостаточно.

`UrlManager.java` содержит базовый auth URL:

```text
https://baseus-us-auth-gw.baseussecurity.com
```

Присутствует выбор региональных серверов. Нельзя считать US-хост правильным
сервером всех последующих запросов для любого аккаунта.

`HeaderInterceptor.java` добавляет `auth`, `platform`, `region`, `appType`,
`appVersion`, `versionCode`, `lang` и другие заголовки. Для подходящих тел запросов
добавляются `timestamp` и `sign`, вычисляемая с участием MD5 тела и SHA1.
XM SDK имеет отдельные заголовки `Authorization`, `Action`, `Signature`,
`SecretId`, `Timestamp`, `Version`. Эти два механизма нельзя смешивать.

В XM HTTP SDK найдены методы/действия:

- `toGetUserDeviceList` / `GetUserDeviceList`;
- `toGetUserDevice` / `GetUserDevice`;
- `toGetDevicesNewThumbnail` / `GetDevicesNewThumbnail`.

Это имена SDK-методов и RPC-действий, **не подтверждённые REST-пути** вида
`/GetUserDeviceList`. Реальные URL и параметры нужно брать из SDK/живой сессии.

## 4. MQTT: команда пробуждения

В `LiveViewModel$wakeUpCamera$1.java` создаётся `XmWakeBean`:

```text
p2p_flag = 1
mode = 1
sn = DeviceConfig.device_sn
productId = DeviceConfig.product_id
wakeKey = PlatformConfig.wake_key
```

Далее вызывается `XmMqttManager.get().wakeupMode(bean)`.

В `com/xm/xm_mqtt/t.java` это превращается в сообщение следующей структуры:

```json
{
  "method": "Action",
  "actionId": "WatchDevice",
  "expands": {
    "productId": "<product_id устройства>",
    "sn": "<серийный номер устройства>",
    "wake_key": "<wake_key из параметров платформы>"
  },
  "payload": {
    "timestamp": 1700000000000,
    "mode": 1
  },
  "seq": "<идентификатор запроса>"
}
```

`timestamp` — текущее время в миллисекундах; показанное число — пример.
`wake_key` SDK добавляет, если значение непустое.
`productId` не следует подменять строкой модели `S0SW00`.

Топик публикации не фиксирован в этом методе: используется
`XmMqttSetting.getPublish()`. Ответ конфигурационного сервиса даёт:

```text
host, port, client_id, username, password, publish, subscribe
```

Это видно в `com/xm/xm_mqtt/h.java`. Получение конфигурации находится в `e.java`:
присутствуют POST и заголовок `Action: GetUserMQTTS` с `Authorization`,
а также альтернативная ветка `GetMQTTWakeSDK`.
В коде есть root discovery URL `https://root.xmitech.net/v1/api/app/host`;
наличие этой ветки не доказывает, что она вызывается для каждого Baseus-аккаунта.

## 5. P2P и снимок

`PlatformConfig.java` содержит:

```text
service_string
device_did
p2p_connect
p2p_password
wake_key
```

`DeviceConfig.a()` создаёт `ParamConnectDev` из серийного номера, канала,
DID, P2P-пароля и product ID.

В актуальной цепочке `LiveViewModel$doConnectP2p$1$2.java`:

```text
ParamConnectP2P.setUdpPort(0)
ParamConnectP2P.setConnectMode(...)
ParamConnectP2P.setServiceString(...)
ParamConnectP2P.setEncryption(true)
ParamConnectP2P.setParserIFrame(true)
XMStreamComCtrl.startConnectDevice(...)
```

Существует также вспомогательный `P2PManager` с отличающимися настройками;
для прототипа ориентироваться следует на цепочку экрана live view.

Кнопка снимка в `LiveFragment.c0(...)` вызывает:

```text
XmMovieViewController.getCurrentMainScreenshot()
AppFileUtil.i(..., bitmap, "....jpg")
```

Таким образом, подтверждённый путь — снимок из декодированной видеосессии.
Наличие `libPPCS_API.so`, XM JNI и видеодекодеров означает, что воспроизведение
этого пути на Python/Windows потребует дополнительной работы с P2P-протоколом
или совместимой библиотекой. Извлечённые `.so` предназначены для Android arm64,
их нельзя просто импортировать в Python на Windows.

## 6. Вариант с превью

`GetDevicesNewThumbnail` стоит проверить первым, если достаточно небольших кадров.
По названию метода нельзя заключать, что он делает новый снимок по запросу.
Возможен возврат последнего загруженного превью/кадра события.

Проверка: изменить сцену перед камерой, запросить превью при спящей камере,
затем после пробуждения и после live-сессии; сравнить содержимое и время кадра.
Не считать время HTTP-ответа временем съёмки.

## 7. Практический план прототипа

1. Уточнить модель/прошивку, регион аккаунта и целевую платформу приложения.
2. Проверить авторизацию и получение данных собственной камеры.
3. Проверить свежесть `GetDevicesNewThumbnail`.
4. Если превью не даёт свежий кадр: проверить MQTT-пробуждение и одну P2P-сессию.
5. После первого нового декодированного кадра сохранить JPEG и обязательно
   закрыть поток/соединение, включая обработку ошибок.
6. Добавить периодический запуск: одна сессия за раз, таймаут, ограниченные
   повторы с увеличением задержки, журнал времени съёмки и ошибок.

Для первого P2P-прототипа Android представляется наиболее прямым путём,
поскольку именно для него уже присутствуют SDK и native-библиотеки.
Доступность SDK для отдельного приложения и ограничения его инициализации
ещё нужно проверить. На Android точные частые фоновые запуски также потребуют
учёта ограничений ОС; WorkManager не подходит для любого произвольного интервала.

Частота пробуждений влияет на батарею. Подходящий интервал следует подобрать
экспериментально, измеряя время активной сессии и расход заряда.

## 8. Где искать исходные свидетельства

Корень Java-кода приложения: `analysis/app-code/sources/com/baseus/`.

| Назначение | Файл относительно этого корня |
|---|---|
| Модели | `modular/http/bean/CurrentDeviceModel.java` |
| Tuya-модели | `modular/http/bean/TuyaDeviceProduct.java` |
| Login endpoint | `modular/http/api/AccountApi.java` |
| Тело login | `modular/http/api/impl/AccountDataApiImpl.java` |
| Заголовки/подпись | `modular/http/interceptor/HeaderInterceptor.java` |
| Серверы | `modular/http/url/UrlManager.java` |
| Пробуждение | `devices/viewmodel/LiveViewModel$wakeUpCamera$1.java` |
| Подключение P2P | `devices/viewmodel/LiveViewModel$doConnectP2p$1$2.java` |
| Параметры устройства | `component/xm/manager/DeviceConfig.java` |
| Параметры платформы | `component/xm/manager/PlatformConfig.java` |
| Сохранение снимка | `devices/fragment/LiveFragment.java`, метод `c0` |

SDK:

- `analysis/sdk-mqtt/sources/com/xm/xm_mqtt/t.java`: сериализация WatchDevice;
- `analysis/sdk-mqtt/sources/com/xm/xm_mqtt/h.java`: разбор MQTT-реквизитов;
- `analysis/mqtt-config/sources/com/xm/xm_mqtt/e.java`: конфигурационный запрос;
- `analysis/sdk-http/sources/com/xmitech/xmapi/XMHttp.java`: API SDK;
- `analysis/sdk-http/sources/com/xmitech/xmapi/m.java`: имена действий/полей.

## 9. Воспроизводимость и ограничения анализа

- `inspect_package.py` распаковывает локальный XAPK/APK и сохраняет инвентарь.
- `index_dex.py` извлекает таблицы строк и определённых классов DEX.
- `setup_jadx.py` загружает JADX 1.5.3 с upstream GitHub.
- JADX использовал Java 21 из установленного DBeaver.
- Полная декомпиляция APK не завершилась в лимит времени. Вместо неё отдельно
  обработаны `classes3.dex`, `classes6.dex`, `classes10.dex` и MQTT-класс `e`.
- Для `classes3.dex` использован режим simple, сообщивший о 17 ошибках.
  Вывод содержит псевдокод с переходами, а не готовые Java-исходники для сборки.
- Даже при завершении JADX отдельные методы могут остаться недекомпилированными.
  Сетевые вызовы выше подтверждены конкретными доступными участками вывода;
  полнота протокола и работоспособность на устройстве пока не подтверждены.
