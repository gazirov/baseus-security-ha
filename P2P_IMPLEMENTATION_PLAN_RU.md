# План автономного клиента Baseus S1

Цель: свежий JPEG на Windows/Linux без Android, затем Home Assistant.
Рабочий ADB backend остаётся без изменений до проверки нового клиента.

## 1. Discovery codecs — выполнено офлайн

- Декодирование `service_string`, извлечение трёх discovery hosts и
  необязательного transport key без вывода секретов.
- DID, vendor sockaddr, P2P-request `0x20`, punch `0x41`, ready `0x42`.
- Сравнение декодера со штатной ARM64-функцией в эмуляторе.
- Побайтная проверка кодеков на двух независимых PCAP.

Файлы: `baseus_s1/ppcs_discovery.py`, `tests/test_ppcs_discovery.py`,
`analysis/verify_ppcs_codecs.py`. Сетевой discovery этим этапом не реализован.

## 2. Discovery и установление peer — следующий этап

Обновление: экспериментальный direct UDP путь добавлен в
`baseus_s1/ppcs_udp.py`; защита пакетов проверена на штатной ARM64-библиотеке.
Relay/TCP fallback не реализован, live gate не пройден.

1. Получить свежий ответ cloud; выбирать P2P-настройки только заданной камеры.
2. Проверить путь с transport key: CRC/proprietary packet protection;
   неизвестный режим отклонять, а не отправлять plaintext по умолчанию.
3. Добавить UDP discovery с ограничением попыток и времени, hello/public
   address, проверкой источника ответов и ограничением числа candidates.
4. Реализовать punching и подтверждение DID/peer. Наличие `F1 42` не считать
   доказательством relay. Не повторять пакеты или endpoint'ы старых сессий.
5. Восстановить relay negotiation отдельно по штатному коду; прямой и relay
   пути должны иметь независимые тесты и явный критерий выбора.

Критерий: fake UDP server проверяет retries, timeout, неверные ответы и чужой
peer; затем один ограниченный live-сеанс подтверждает установленный transport.
Текущий live-блокер: `GetUserDevice` вернул HTTP 500. OUT содержит только
исходящие PPCS-пакеты, поэтому ответы клиента нельзя восстановить из него.

## 3. Reliable UDP session

Обновление: single-threaded `UdpSession` объединяет окна DRW, ACK, keepalive,
отбрасывание чужого source и закрытие socket. Проверен настоящий localhost UDP
обмен. Совместимость с живой камерой ещё не подтверждена.

Соединить `SendWindow`/`ReceiveWindow` с выбранным peer: ACK, фрагментация,
повторы, независимые каналы, keepalive и корректное закрытие. Проверять peer
до ACK и обработки payload. Ограничивать очереди и память всей сессии.

Критерий: тесты потерь, reorder, дубликатов, wraparound, чужого адреса,
исчерпания retries и освобождения socket после ошибки; live command response.

## 4. XM login и media

Обновление: `xm_stream.py` и `autonomous.py` реализуют обычные XM frames,
AES ECB/CBC, login и актуальные поля видео `camera_sn`/`videoKeepAlive_2`.
Сборка media headers проверена на IN; login reply пока проверен синтетически.

Перепроверить AES/helper и поля start-video по SDK. Отправить login по каналу
0, проверить ответ, затем open-video. Восстановить полные media frames по
каналам 1/2, проверить отдельный media cipher и определить реальный кодек.

Критерий: успешный login и подтверждённый elementary video stream. Биты
encryption type сами по себе не подтверждают ключ или алгоритм media.

## 5. JPEG и Home Assistant

Обновление: добавлены PyAV JPEG backend и `baseus_autonomous.py` для login,
списка камер и capture-all. H.264→JPEG проверен синтетическим stream; снимок
живой камеры и HA остаются обязательными следующими этапами.

Добавить backend с ограниченным ожиданием keyframe, декодированием через
FFmpeg/PyAV, проверкой JPEG и атомарным сохранением. Затем CLI выбора backend,
wake при необходимости, повторное подключение и публикация свежего кадра в HA.

Критерий: несколько свежих кадров из новых сессий без планшета, включая
сценарий сна/пробуждения; регрессионные тесты существующего ADB backend.

## Общие ограничения

Не логировать и не сохранять токены, P2P-пароль, wake key, service string,
transport key и MQTT credentials. PCAP остаются локально. Не считать офлайн
совпадение форматов доказательством работоспособности сетевого клиента.
