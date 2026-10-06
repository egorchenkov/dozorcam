# Первичная активация камер разных вендоров

*Исследование 03.10.2026. Камеры не трогали: всё ниже — официальная документация,
открытые реализации и вторичные источники. Пометки: **[док]** — подтверждено
документацией или кодом по ссылке; **[вывод]** — косвенно или предположение.
Что не нашлось публично — так и написано. Заводские IP-адреса камер намеренно
не приводятся (гейт публичного экспорта не пропускает частные сети) — они есть в источниках.*

## Что умеет Dozorcam сейчас

- **Hikvision/HiWatch** — автоактивация в /add (`cctv/engine/hikvision_activation.py`):
  признак без пароля `GET /SDK/activateStatus` + SADP, протоколы V3 и legacy.
- **Все остальные** — опрос сети узнаёт марку по тому, что камера отдаёт без пароля
  (`cctv/engine/vendor_setup.py`): заголовок Server/WWW-Authenticate RTSP и HTTP,
  страница `GET /`. Камера помечается `activation="manual"`, если
  - есть явный признак «ждёт настройки» — пока только **Axis** (`systemready.cgi`
    → `needsetup: yes`, анонимно по документации VAPIX), или
  - у камеры известной марки открыт только веб-интерфейс, без RTSP и ONVIF
    (у новых камер потоки поднимаются после задания пароля).
- Мастер /add показывает таким камерам отдельное сообщение: инструкцию по каждой
  марке (`setup.<марка>` в `cctv/i18n/locales/*.json`) и кнопку «🔑 Пароль задан» на
  камеру, которая ведёт в обычный путь «логин и пароль». Кнопки «Активировать» у них нет.
- **Попыток входа нет ни в каком виде, даже с пустым паролем:** у Dahua, Uniview и
  Hanwha неудачные входы считаются, после нескольких учётка блокируется. Проверка
  «пароль по умолчанию» — это отдельное осознанное решение (см. рекомендацию).

## Сводная таблица

| Вендор | Первый пароль | Протокол / эндпоинт | Признак «новая» без логина | SDK / облако | Автоматизация локально | ONVIF/RTSP после | Источники |
|---|---|---|---|---|---|---|---|
| **Hikvision / HiWatch** | Активация обязательна примерно с 2015 [док] | ISAPI: `/ISAPI/Security/challenge` → `PUT /ISAPI/System/activate`; V3 `GetChallengeV3`/`StartActivateV3`; SADP UDP 37020 [док] | `GET /SDK/activateStatus` [док] | Не нужны | **Да, есть в Dozorcam** | RTSP включён; ONVIF с прошивок 5.5 выключен, отдельный ONVIF-пользователь [док] | 1, 2 |
| **Dahua** (RVi и др. OEM) | Инициализация обязательна примерно с 2017, admin/admin убран [док] | Веб-мастер, ConfigTool, регистратор. Discovery DHDiscover.search UDP 37810 (JSON за DHIP-заголовком) [док]; в трафике инициализации — `client.notifyEncryptInfo` (RSA+AES) и `client.notifyDevInit` [док, issue]; RPC2 официально не документирован | ConfigTool показывает «Uninitialized», поле ответа публично **неизвестно** | Официально — ConfigTool/SDK | **Частично**: discovery открыт, init — реверс по дампу ConfigTool | RTSP обычно включён [вывод]; ONVIF на новых прошивках выключен (Access Platform) [док, вторичн.] | 3–6 |
| **Imou** (Dahua) | Safety Code с наклейки; приложение обычно меняет [вторичн.] | Приложение Imou Life; можно ConfigTool [вторичн.] | Неизвестно | Фактически приложение/облако | **Частично** (как Dahua) | RTSP/ONVIF включить в приложении [вторичн.] | 7 |
| **Uniview** | Старые: admin/123456 с принудительной сменой; часть новых — «inactive» без пароля, NVR UNV активирует своим [вторичн.] | Веб, EZTools, NVR. Эндпоинт активации в LAPI публично **неизвестен**; открыт `/LAPI/V1.0/System/DeviceInfo` | Неизвестно | EZTools закрыт, LAPI частично открыт | **Частично**: смена admin/123456 — дёшево, путь «inactive» — реверс | ONVIF обычно включён [вывод] | 8–10 |
| **Tantos** (РФ, OEM) | Старые TSi: admin/admin [вторичн.] | По платформе-донору (Hikvision / Dahua / прочие); публичной карты «линейка → донор» нет | Как у донора | Как у донора | По отпечатку платформы (см. ниже) | ONVIF заявлен | 11, 12 |
| **Axis** (AXIS OS 10+) | Пароля нет; VAPIX и ONVIF закрыты до первого пользователя [док] | `POST /axis-cgi/pwdgrp.cgi?action=add&user=root&pwd=…&grp=root&sgrp=admin:operator:viewer:ptz` (с OS 11.5 имя любое) [док] | `POST /axis-cgi/systemready.cgi`, метод `systemready` → **`needsetup: yes`** (OS ≥ 9.50) [док] | Не нужны | **Да**, по открытой документации | RTSP — с VAPIX-пользователем; ONVIF — отдельный пользователь через `/vapix/services` CreateUsers [док] | 13, 14 |
| **Hanwha Vision (Wisenet)** | Пароля нет, первый вход заставляет задать [док] | SUNAPI (HTTP/S) + UDP 7701/7711: «Discovery, IP setting, Initial PW» [док, whitepaper]; имя CGI первого пароля публично **неизвестно** | Device Manager видит «без пароля»; поле **неизвестно** | Не нужны (SUNAPI — партнёрам) | **Частично**: discovery в open source, пароль — реверс | Обычно включены [вывод] | 15, 16 |
| **Reolink** | admin с пустым паролем («uninitialized»); у новых моделей HTTP/RTSP/ONVIF закрыты, открыт только TCP 9000 (Baichuan) [док, open source] | Baichuan :9000 (вход), пароль — HTTP `api.cgi` `ModifyUser` [open source] | Вход с пустым паролем по Baichuan (это попытка логина) | Не нужны | **Да**: `reolink-init` (MIT, Python, на reolink_aio) | После init RTSP/ONVIF/HTTP **включать явно** [док] | 17–19 |
| **TP-Link VIGI** | Пароля нет, активация обязательна (веб по https, VIGI Security Manager, приложение) [док] | Локальный HTTP JSON (`/stok=…/ds`, вход с RSA) отреверсен; метод активации **неизвестен** | Неизвестно | Не нужны | **Частично** | ONVIF :2020 и RTSP :554 с учёткой admin [док] | 20–22 |
| **EZVIZ** (Hikvision) | Активации нет: пароль — Verification code с наклейки [вторичн.] | Только приложение, веб-интерфейса нет | Не нужен | Приложение/облако | **Нет** (нужен код с наклейки) | RTSP выключен, включается в приложении (LAN Live View); ONVIF не у всех [вторичн.] | 23 |
| **Milesight** | До V4x.7.0.69 admin/ms1234; с неё — обязательная активация (пароль 8–32 + контрольные вопросы) [док] | Веб, Smart Tools ≥ 2.4.0.1, CMS, NVR; API **неизвестен** | Smart Tools показывает «Inactive», поле **неизвестно** | Не нужны | **Частично / не исследовано** | Не указано | 24 |
| **TVT** (+ OEM, TruVision S5) | Старые: admin/123456 или пустой; новые — окно Activation [док] | Веб, IPTool / TruVision Device Manager; API **неизвестен** | Неизвестно | Не нужны | **Частично** | Не указано | 25, 26 |
| **Xiongmai** (XMEye/Sofia, много OEM: Optimus и др.) | admin с **пустым** паролем; «default» убран после 08.2018; смену навязывает приложение, не камера [док, вторичн.] | DVRIP TCP 34567 (хеш Sofia), `changePasswd` [open source] | Вход admin/"" (попытка логина) | Не нужны | **Да**: OpenIPC/python-dvr | RTSP обычно есть, ONVIF урезан [вывод] | 27, 28 |
| **Ajax** (TurretCam/BulletCam) | Пароля нет: mTLS и привязка к хабу через приложение [док] | Только приложение Ajax + облако | — | **Облако обязательно** | **Нет** | ONVIF (прошивка ≥ 2.356) после включения в приложении, до 5 ONVIF-пользователей [док] | 29, 30 |
| RVi (РФ) | Старые admin/admin — Dahua-OEM [вторичн. + вывод] | Как у Dahua | — | — | Как у Dahua | — | 31 |
| Trassir TR-D | admin/admin [вторичн.] | Веб | — | — | Да, если пароль по умолчанию | RTSP 554 | 32 |

**Отпечаток платформы OEM (Tantos, RVi, Optimus и т. п.)** — пассивно, по портам и
анонимным эндпоинтам [вывод]: 8000 + SADP 37020 + `/SDK/activateStatus` → Hikvision;
37777 / UDP 37810 → Dahua; 34567 → Xiongmai; 9000 → Reolink; `/LAPI/` → Uniview;
UDP 7701 → Hanwha; 6036 → TVT.

## Рекомендация: порядок поддержки

1. **Axis** — всё по открытой документации: анонимный `needsetup` уже читается,
   осталось `pwdgrp.cgi` + ONVIF-пользователь через `/vapix/services`. Работы на день,
   стенд не обязателен (эмулятор по документации), риск блокировки нулевой.
2. **Reolink** — готовая MIT-реализация на Python; без этого шага камера вообще не отдаёт
   RTSP. Нужно: порт 9000 в развёртку, вход admin/"" как *проверка* (единственная
   попытка), `ModifyUser`, включение RTSP/ONVIF.
3. **Dahua / RVi / Imou** — второй по распространённости в РФ. DHDiscover открыт,
   инициализация (`notifyEncryptInfo`/`notifyDevInit`) — реверс по дампу ConfigTool:
   **нужна живая Dahua на стенде**. Самый ценный, но и самый дорогой шаг.
4. **Xiongmai и OEM** — пустой пароль + python-dvr (DVRIP `changePasswd`). Дёшево; в коде
   явно оговорить, что «вход пустым паролем» и есть проверка состояния.
5. **Uniview** — смена admin/123456 дешёвая, режим «inactive» — реверс EZTools.
6. **Hanwha, VIGI, Milesight, TVT** — discovery частично открыт, установка пароля не
   документирована; реверс при появлении конкретной камеры.
7. **EZVIZ, Ajax** — не делать: код с наклейки или облако. Достаточно инструкции (уже есть).

**Правило для всех будущих веток:** сначала пассивный отпечаток (порты, анонимные
эндпоинты); попытка входа паролем по умолчанию — ровно одна и только у вендоров, где
он документирован и неудача не блокирует учётку; пароль — в хранилище до обращения к
камере, как у Hikvision (`activation-vault.json`).

## Что проверить на живых камерах

- Признаки марки в `vendor_setup.BRAND_HINTS` собраны по документации и типовым
  веб-интерфейсам, а не по снятым ответам: на первой живой камере каждой марки сверить
  заголовки и `GET /`. «Чужой» адрес с одним веб-интерфейсом попадает в поиск только
  при узнанной марке; признаки нарочно характерные («tp-link» и «web service» есть и у
  роутеров — не используются).
- Новые Reolink с одним портом 9000 опрос сейчас не видит (9000 не в `SCAN_PORTS`).
- Не найдено публично: поле «uninitialized» в ответе DHDiscover; эндпоинты активации
  Uniview (LAPI), Hanwha, VIGI, Milesight, TVT; карта платформ Tantos по линейкам.
  Нужны дампы трафика вендорских утилит на реальном устройстве.

## Источники

1. https://community.home-assistant.io/t/hikvision-camera-activation-through-isapi/588261
2. https://securitycamcenter.com/enable-onvif-hikvision-cameras/
3. https://github.com/rroller/dahua/issues/473
4. https://github.com/rroller/dahua/pull/798
5. https://material.dahuasecurity.com/download/Initialization_and_password_reset_for_storage_devices_V3_EN_20171115.pdf
6. https://ivsecurity.com.au/wp-content/uploads/documents/Dahua%20Camera%20ONVIF%20Configuration%20-%20IVSEC.pdf
7. https://cln.io/blog/getting-local-rtsp-streams-from-the-imou-cruiser-dual-8mp/
8. https://www.univiewtechnology.com/wp-content/uploads/2024/05/UVT-Network-Camera-User-Manual-v3.05-rev1.pdf
9. https://www.clevelandsecuritycameras.com/post/what-is-the-uniview-default-password-2026-guide
10. https://www.niceforyou.com/sites/default/files/2024-07/240611-UNV-External-Interface-Documentation_V1.10-EN.pdf
11. https://tantos.pro/manuals/Quick%20install%20guide%20TANTOS%20TSi%20cameras.pdf
12. https://tantos.pro/news/klientskoe-programmnoe-obespechenie-pod-apple-mac-os-x-dlya-nvr-i-dvr-tantos.html
13. https://developer.axis.com/vapix/network-video/systemready-api/
14. https://www.axis.com/files/manuals/Initial_Device_Access_Changes.pdf
15. https://www.hanwhavision.com/wp-content/uploads/2024/01/240105_Device-Manager_White-Paper_EN.pdf
16. https://github.com/melchi45/wisenet-camera-discovery
17. https://github.com/TinKurbatoff/reolink-init
18. https://support.reolink.com/articles/900000603563-Introduction-to-the-Default-User-and-Password-of-Reolink-Cameras-NVRs/
19. https://www.aegisgates.com/articles/how-to-enable-rtsp-reolink-camera-setup
20. https://www.tp-link.com/au/support/faq/2898/
21. https://www.tp-link.com/us/user-guides/vigi-security-manager/chapter-2-add-devices-to-vigi-security-manager.html
22. https://github.com/toast-riot/TP-Link-Camera-API
23. https://community.home-assistant.io/t/ezviz-rtsp-enable/736416
24. https://support.milesight.com/support/solutions/articles/69000797899-how-to-activate-and-set-the-security-question-for-milesight-devices
25. https://www.sielinvest.ro/products/MANUAL%20DE%20UTILIZARE/TVT-5501.pdf
26. https://www.videoexpertsgroup.com/glossary/tvt-login-default-ip-username-password-port
27. https://github.com/OpenIPC/python-dvr
28. https://krebsonsecurity.com/2018/10/naming-shaming-web-polluters-xiongmai/
29. https://ajax.systems/products/turretcam/
30. https://support.ajax.systems/en/manuals/onvif/
31. https://files.layta.ru/upload/files_upload/RVI/Instruktsii/Instukziya_po_nastrojke_IP-videokamer_RVi_cherez_%20web_interfejs.pdf
32. https://videoglaz.ru/upload/manuals/trassir/TR_DTRsd_UM.pdf
