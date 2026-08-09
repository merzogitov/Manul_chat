# 🐱 Манул Чат 1.0

Первая стабильная версия приватного веб-мессенджера **Манул Чат**.

<p align="center">
  <img src="static/image/logo_max.png" width="350">
</p>

## Скриншоты

<details>
<summary>📷 Показать скриншоты</summary>

<br>

### Авторизация

<p align="center">
  <img src="screenshot/003.png" width="500">
</p>

### Чат на компьютере

<p align="center">
  <img src="screenshot/001.png" width="800">
</p>

<p align="center">
  <img src="screenshot/002.png" width="800">
</p>

### Чат на телефоне

<p align="center">
  <img src="screenshot/004.jpg" width="300">
</p>

</details>


## Возможности

- приватные диалоги 1-на-1;
- сообщения в реальном времени через WebSocket;
- online/offline статусы;
- история сообщений в SQLite;
- изображения JPG / PNG / WebP / GIF;
- ответы на сообщения;
- очистка переписки только для текущего пользователя;
- административная панель;
- PWA для Android и iPhone/iPad;
- Web Push уведомления;
- HTTPS через reverse proxy;
- Docker / Synology Container Manager.

## Docker

Порт приложения:

```yaml
ports:
  - "0.0.0.0:18080:8000"
```

То есть приложение доступно по порту `18080` на всех сетевых интерфейсах сервера.

Если сервер находится в локальной сети, доступ будет возможен по адресу вида:

```text
http://IP_СЕРВЕРА:18080
```

Для доступа из Интернета рекомендуется использовать HTTPS через reverse proxy.

## Постоянные данные

В `compose.yaml` подключены:

```yaml
volumes:
  - ./data:/app/data
  - ./static:/app/static
```

### data/

Содержит постоянные данные:

```text
data/messenger.db
data/uploads/
data/vapid_private.pem
```

Папку `data/` нельзя удалять при обновлении.

### static/

Содержит интерфейс:

```text
static/chat.html
static/login.html
static/admin.html
static/style.css
static/service-worker.js
static/image/
```

`static/` подключён отдельным bind mount. Поэтому изменения HTML/CSS/JS можно применять **без пересборки Docker**.

После изменения интерфейса обычно достаточно:

```text
Ctrl + F5
```

или повторно открыть установленное PWA.

## Когда нужен Build / Rebuild

Пересборка Docker требуется при изменении:

```text
app.py
requirements.txt
Dockerfile
compose.yaml
```

Для изменений только внутри:

```text
static/
```

пересборка не нужна.

## PWA

### Android

Открыть сайт в Chrome:

```text
⋮ → Установить приложение
```

### iPhone / iPad

Открыть сайт в Safari:

```text
Поделиться → На экран Домой
```

## Push-уведомления

В интерфейсе кнопка показывает состояние:

```text
🔔 Вкл
```

или:

```text
🔕 Выкл
```

Push работает для текстовых сообщений и изображений.

VAPID-ключ хранится в:

```text
data/vapid_private.pem
```

Не удаляйте его при обновлениях.

## Версия

```text
Манул Чат 1.0
```
