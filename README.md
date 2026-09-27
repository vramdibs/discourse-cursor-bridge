# discourse-cursor-bridge

OpenAI-совместимый proxy к **`cursor-agent`** (`POST /v1/chat/completions`, порт **8787**) и мост **Discourse -> Cursor agent** (webhook `@mention`).

## Связанные проекты

- [tgzh](https://github.com/vramdibs/tgzh) - Telegram-бот проверки ДЗ; использует этот bridge для `/chat` и «Проверить ещё раз (Cursor)». Контракт на стороне клиента: [docs/integration.md](https://github.com/vramdibs/tgzh/blob/main/docs/integration.md)

## Discourse webhook

Когда в `discuss.example.com` ты упоминаешь
`@qwen_bot`, Discourse шлёт webhook на этот сервис. Сервис:

1. проверяет HMAC и allowlist пользователей,
2. подгружает контекст топика (последние N постов с raw),
3. запускает локальный `cursor-agent -p --output-format json --force --trust --workspace ~/workspace`
   (тот же агент, что и в IDE — видит `.cursor/rules/`, MCP, твой репо),
4. постит ответ в исходный топик через Discourse API

Файлы:

- [server.py](server.py) — FastAPI HTTP-сервер, эндпоинты `POST /discourse/webhook`, `GET /healthz` и (опционально) `POST /v1/chat/completions` (см. ниже).
- [openai_endpoint.py](openai_endpoint.py) — OpenAI-совместимый `POST /v1/chat/completions` поверх `cursor-agent` для внешних клиентов (например, fallback-LLM tgzh-бота).
- [config.py](config.py) — загрузка `.env` (этот каталог) + внешний `discourse.env` (путь в `DISCOURSE_ENV_FILE`).
- [discourse_client.py](discourse_client.py) — обёртка над Discourse REST: `get_topic`, `create_post`, retry на 429.
- [agent_runner.py](agent_runner.py) — subprocess `cursor-agent` с очередью (FIFO, 1 воркер) и таймаутом.
- [prompt.py](prompt.py) — сборка промпта из контекста топика.
- [state.py](state.py) — дедуп уже обработанных `post_id` в `state.json`.
- [scripts/setup.sh](scripts/setup.sh) — venv + зависимости.
- [scripts/smoke-webhook.sh](scripts/smoke-webhook.sh) — локальный smoke-test без Discourse.
- [systemd/discourse-cursor-bridge.service](systemd/discourse-cursor-bridge.service) — systemd unit для моста.
- [systemd/discourse-cursor-tunnel.service](systemd/discourse-cursor-tunnel.service) — systemd unit для cloudflared.

## Установка

```bash
git clone https://github.com/vramdibs/discourse-cursor-bridge.git
cd discourse-cursor-bridge
bash scripts/setup.sh        # создаст .venv и .env
$EDITOR .env                 # выставь WEBHOOK_SECRET (длинная случайная строка) и ALLOWED_USERNAMES
```

Отдельно подготовь `discourse.env` (путь в `DISCOURSE_ENV_FILE`): `DISCOURSE_URL`,
`DISCOURSE_API_KEY`, `DISCOURSE_USERNAME`. Бот будет постить от имени этого пользователя
(поменяй `BOT_USERNAME` в `.env`, если нужен другой).

## Запуск

Локально:

```bash
.venv/bin/python server.py
# слушает 127.0.0.1:8787, healthcheck:
curl http://127.0.0.1:8787/healthz
```

Под systemd (user-режим, без sudo):

```bash
mkdir -p ~/.config/systemd/user
ln -sf ./systemd/discourse-cursor-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discourse-cursor-bridge.service
journalctl --user -u discourse-cursor-bridge.service -f
```

Чтобы юнит пережил logout: `loginctl enable-linger $USER`.

## OpenAI-совместимый эндпоинт `/v1/chat/completions` (опционально)

Помимо основного Discourse-флоу, мост может выставить тонкий
OpenAI-совместимый эндпоинт `POST /v1/chat/completions`, который под
капотом дёргает `cursor-agent`. Сценарий — **fallback-LLM**: например,
бот [tgzh](https://github.com/vramdibs/tgzh) держит основным `vLLM (qwen)`, а сюда уходит
только когда основной эндпоинт недоступен.

Эндпоинт **выключен по умолчанию**: пока в `.env` пуст `BRIDGE_OPENAI_API_KEY`,
сервер на этот путь отвечает `503`.

Конфигурация (`.env`):

```bash
BRIDGE_OPENAI_API_KEY=<длинный_секрет>
CURSOR_OPENAI_WORKSPACE=/tmp/cursor-openai-sandbox    # отдельный пустой workspace
BRIDGE_OPENAI_TIMEOUT_SEC=                            # пусто => наследуем AGENT_TIMEOUT_SEC
BRIDGE_OPENAI_MAX_PROMPT_CHARS=200000
```

Авторизация: заголовок `Authorization: Bearer <BRIDGE_OPENAI_API_KEY>`.

Поддерживаются:

- `messages[*].content` как `str` или массив `[{type:"text"|"image_url"}]`.
  Для `image_url` принимаются только `data:image/...;base64,...` — каждое
  изображение пишется во временный файл в `CURSOR_OPENAI_WORKSPACE/.openai-images/`,
  путь подставляется в собранный prompt; после ответа файл удаляется.
- Все роли склеиваются в один prompt с маркерами `[system]/[user]/[assistant]` —
  у `cursor-agent` нет ролей.
- `stream=true` пока возвращает `400` (не нужен текущим клиентам).

Sandbox-семантика: `cursor-agent` запускается в **отдельном** workspace
(`CURSOR_OPENAI_WORKSPACE`), не в `~/workspace`, чтобы внешние
запросы не имели доступа к рабочему репо. Это компромисс: с `--force --trust`
агент всё ещё может ходить в инструменты — поэтому ключ от эндпоинта
храните как обычный API-token и не раздавайте за пределы доверенного
сервиса. Подробности и обоснование — [DECISIONS.md](DECISIONS.md).

Smoke-тест:

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer $BRIDGE_OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"cursor-agent","messages":[{"role":"user","content":"скажи привет"}]}' \
  | jq .
```

## Тоннель наружу

Discourse живёт публично, твоя машина — в локалке. Нужен публичный URL,
проксирующий на `127.0.0.1:8787`.

### Вариант A — cloudflared (рекомендуется)

Quick start (рандомный `*.trycloudflare.com` каждый запуск, ок для теста):

```bash
sudo apt install cloudflared    # или скачать .deb с github.com/cloudflare/cloudflared
cloudflared tunnel --url http://127.0.0.1:8787
```

Постоянный hostname (для прода) — именованный тоннель:

```bash
cloudflared tunnel login                                    # авторизация в Cloudflare
cloudflared tunnel create discourse-cursor-bridge
cloudflared tunnel route dns discourse-cursor-bridge cursor-bridge.example.com
cat > ~/.cloudflared/config.yml <<YML
tunnel: discourse-cursor-bridge
credentials-file: $HOME/.cloudflared/<UUID>.json
ingress:
  - hostname: cursor-bridge.example.com
    service: http://127.0.0.1:8787
  - service: http_status:404
YML
# Затем подменить ExecStart в systemd/discourse-cursor-tunnel.service
# на: ExecStart=/usr/bin/env cloudflared tunnel run discourse-cursor-bridge
ln -sf ./systemd/discourse-cursor-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discourse-cursor-tunnel.service
```

### Вариант B — SSH reverse tunnel на VPS Discourse

Если есть SSH к VPS `discuss.example.com`:

```bash
# на этой машине
ssh -N -R 127.0.0.1:8787:127.0.0.1:8787 user@discourse-vps
# на VPS добавить в nginx (внутри server { listen 443 ... }):
#   location /discourse-cursor-webhook {
#     proxy_pass http://127.0.0.1:8787/discourse/webhook;
#   }
```

Тогда payload URL для webhook будет
`https://discuss.example.com/discourse-cursor-webhook`.

## Настройка webhook в Discourse

Admin -> API -> Webhooks -> New Webhook:

- **Payload URL**: `https://cursor-bridge.example.com/discourse/webhook`
  (или `https://discuss.example.com/discourse-cursor-webhook` для варианта B).
- **Content type**: `application/json`.
- **Secret**: то же значение, что `WEBHOOK_SECRET` в `.env`.
- **Which events should trigger this webhook?** — `Post Event` (только он).
- **Categories filter** (опц.): начни с одной (например `6 - черновики`),
  чтобы не плеснуть лишних запросов; потом убери ограничение.
- **Verify SSL**: включить (для cloudflared или nginx работает).
- **Active**: включить.

После сохранения нажми `Ping` — должен прилететь `204 No Content` (наш
healthcheck `GET /healthz` возвращает 200, а Discourse Ping шлёт `ping` event,
который мы корректно игнорируем).

## Как пользоваться

Создай пост или ответ в любом топике с `@qwen_bot ...твой запрос...`. Через
несколько секунд (пока крутится агент) бот ответит новым постом в этом же
топике, с `reply_to_post_number` на твоё сообщение.

## Безопасность

- `--force --trust` означают, что агент может писать в файлы и запускать
  shell. Никогда не убирай `WEBHOOK_SECRET` и `ALLOWED_USERNAMES` —
  это единственное, что отделяет интернет от твоего ноутбука.
- Логи всех запусков — в `logs/YYYY-MM-DD.jsonl`.
- Дедуп уже обработанных post_id — `state.json`.

## Локальный smoke-тест

```bash
# в одном терминале:
.venv/bin/python server.py

# в другом:
WEBHOOK_SECRET=$(grep ^WEBHOOK_SECRET= .env | cut -d= -f2-) \
USERNAME=admin \
scripts/smoke-webhook.sh 999999 8888888 1 "Привет @qwen_bot, скажи 'pong' одним словом"
# проверь в логах сервера, что агент стартанул;
# попытка постинга в реальный Discourse уйдёт - подставь настоящий topic_id.
```

## Эксплуатация (cheat sheet)

### Первый запуск под systemd --user (один раз)

`systemctl --user` ищет юниты только в `~/.config/systemd/user/`, не в произвольной папке. Поэтому делаем симлинки:

```bash
mkdir -p ~/.config/systemd/user
ln -sf ./systemd/discourse-cursor-bridge.service ~/.config/systemd/user/
ln -sf ./systemd/discourse-cursor-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger $USER        # чтобы юнит пережил logout
systemctl --user enable --now discourse-cursor-bridge.service
curl -sS http://127.0.0.1:8787/healthz | python3 -m json.tool
```

### Перед боевым запуском - проверь чеклист

1. `.env` -> `WEBHOOK_SECRET` это длинная случайная строка (>= 32 символа), `ALLOWED_USERNAMES` содержит твой реальный логин в discuss.example.com.
2. `discourse.env` (см. `DISCOURSE_ENV_FILE`) -> `DISCOURSE_USERNAME` совпадает с `BOT_USERNAME` из `.env`.
3. `cloudflared` установлен (`which cloudflared`) и тоннель поднят - даёт публичный URL.
4. В Discourse Admin -> API -> Webhooks создан хук на `<публичный URL>/discourse/webhook` с тем же `WEBHOOK_SECRET`, событие `Post Event`, для теста ограничен категорией `6 - черновики`. Кнопка `Ping` отдаёт 200.
5. Боевая проверка: в тестовом топике пишешь `@qwen_bot скажи pong одним словом`, в логах видно `replied to post N -> reply id=...`.

### Тоннель (cloudflared)

```bash
sudo apt install cloudflared
cloudflared tunnel --url http://127.0.0.1:8787    # быстрый тест (рандомный URL)

# постоянный hostname (после cloudflared tunnel login + create + route, см. раздел "Тоннель наружу" выше):
systemctl --user enable --now discourse-cursor-tunnel.service
journalctl --user -u discourse-cursor-tunnel.service -f
```

### Управление мостом

```bash
systemctl --user status discourse-cursor-bridge.service          # состояние
systemctl --user restart discourse-cursor-bridge.service         # после правки .env
systemctl --user stop    discourse-cursor-bridge.service         # остановить
systemctl --user disable --now discourse-cursor-bridge.service   # выключить и не стартовать при логине
journalctl --user -u discourse-cursor-bridge.service -f          # стрим логов сервиса
journalctl --user -u discourse-cursor-bridge.service --since "10 min ago"
tail -f logs/$(date -I).jsonl                                    # структурированный лог запросов (skip / agent_done / replied / error)
```

### Локальный healthcheck и smoke

```bash
curl -sS http://127.0.0.1:8787/healthz | python3 -m json.tool
.venv/bin/python scripts/smoke.py                                # e2e со stub-ами Discourse и fake-cursor-agent
```

### Разовая отправка фейкового webhook на живой мост

```bash
WEBHOOK_SECRET=$(grep ^WEBHOOK_SECRET= .env | cut -d= -f2-) \
USERNAME=admin \
scripts/smoke-webhook.sh <topic_id> <post_id> <post_number> "@qwen_bot пинг"
```

### Если бот молчит - что смотреть в логах

В `journalctl` ищи:

- `rejected webhook: bad signature` -> `WEBHOOK_SECRET` в `.env` и в Discourse webhook не совпадают.
- `ignored ... reason: user_not_allowed:<имя>` -> добавь `<имя>` в `ALLOWED_USERNAMES` или ставь под нужным юзером.
- `ignored ... reason: no_mention` -> в посте нет `@qwen_bot` (или у бота другой `BOT_USERNAME`).
- `ignored ... reason: self_post` -> сработала анти-цикл защита (это норма для собственных ответов бота).
- `ignored ... reason: duplicate` -> webhook прилетел повторно, мост уже отвечал (см. `state.json`, можешь почистить).
- `agent did not produce a usable answer` -> агент упал/ушёл в таймаут; подними `AGENT_TIMEOUT_SEC`, посмотри `stderr_tail` в `logs/<date>.jsonl`.
- `failed to post reply` -> Discourse вернул не-2xx; проверь `DISCOURSE_API_KEY` в `bridge/discourse.env` и права `qwen_bot` в категории.

### Полный сброс дедупа (если нужно перезапустить обработку)

```bash
systemctl --user stop discourse-cursor-bridge.service
rm -f state.json
systemctl --user start discourse-cursor-bridge.service
```
