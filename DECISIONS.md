# Архитектурные решения discourse-cursor-bridge

Журнал ключевых решений по мосту Discourse -> Cursor agent.
Формат - кратко: контекст, решение, последствия.

## ADR-001: Cursor CLI на этой машине, не Cloud Agents

- **Дата:** 2026-04-17
- **Контекст:** Нужно дать ассистенту в Discourse доступ к локальному репо
  `~/workspace` со всеми правилами `.cursor/rules/`, MCP-серверами и
  свежим состоянием файлов. Альтернативы - Cursor Cloud / Background Agents
  (привязка к GitHub-репо, runs в облаке) или сторонняя LLM с собственным
  RAG поверх репозитория.
- **Решение:** Запускать `cursor-agent -p` локально через subprocess. Бридж
  живёт на этой же машине (`~/.local/bin/cursor-agent`,
  `--workspace ~/workspace`).
- **Последствия:**
  - Агент видит весь актуальный workspace, в т.ч. приватные файлы (`career/`,
    `discourse.env`).
  - Машина обязана быть онлайн и доступна снаружи через тоннель
    (cloudflared / SSH reverse). Локальное правило
    [.cursor/rules/local-network-first.mdc](../../.cursor/rules/local-network-first.mdc)
    напоминает: внутренние URL стоит сперва пробовать с этой машины.
  - `--force --trust` дают агенту право писать в файлы и запускать shell ->
    единственный замок от внешнего мира - HMAC + allowlist (см. ADR-003).

## ADR-002: Триггер - @-упоминание бота в любом посте

- **Контекст:** Discourse умеет webhook'и на `post_created`/`post_edited`,
  чат, PM, выделенные категории. Хотелось максимально натурально влиться в
  обычные дискуссии, без отдельной "комнаты для бота".
- **Решение:** Discourse webhook на Post Event -> bridge фильтрует посты, в
  `raw` которых есть `@<BOT_USERNAME>` (case-insensitive).
- **Последствия:**
  - Один и тот же бот доступен в любом топике/категории.
  - Защита от шума - allowlist `ALLOWED_USERNAMES` (бот реагирует только на
    упоминания от конкретных юзеров; чужой `@<BOT>` логируется и
    игнорируется).
  - Анти-цикл: посты от самого бота отбрасываются по `username`.
  - Нет проблемы случайного срабатывания на цитату - стандартная Discourse
    цитата не сохраняет mention.

## ADR-003: Не трогать глобальный `qwen_bot`, ввести отдельного `cursor_bot`

- **Контекст:** На `discuss.example.com` уже стоит юзер `Qwen_bot`
  (id=-1220), который слушает упоминания `@Qwen_bot` локальной Qwen-моделью
  (видимо `discourse-ai` plugin). И на `qwen_bot` завязаны существующие
  скрипты `publish-*.sh` для постов от лица того же юзера. Если бридж
  отвечает с того же ника, мы (а) перебиваем существующего бота,
  (б) пользователь не отличает кто ответил.
- **Решение:** Создан отдельный юзер `cursor_bot` (id=23, группа `Bot`,
  имя "Cursor Agent") с собственным API ключом. В
  [discourse.env](../../discourse.env) добавлены переменные
  `DISCOURSE_USERNAME_CURSOR=cursor_bot` и `DISCOURSE_API_KEY_CURSOR=...`,
  старые `DISCOURSE_USERNAME=qwen_bot` и `DISCOURSE_API_KEY=...`
  оставлены без изменений. В
  [config.py](config.py) бридж предпочитает `*_CURSOR` с fallback на
  глобальные.
- **Последствия:**
  - Триггеры разделены: `@Qwen_bot` идёт к старому Discourse-AI боту,
    `@cursor_bot` - к нашему мосту.
  - `publish-*.sh` продолжают постить от `qwen_bot`, ничего не сломано.
  - В UI пользователь сразу видит, кто ответил, по аватару и нику.
  - Появление нового бота требует только добавления отдельных переменных,
    не трогая legacy конфиг.

## ADR-004: stream-json + ранний exit вместо `--output-format json`

- **Контекст:** `cursor-agent --print` в текущей версии (`2026.04.08`)
  имеет известный баг: после получения ответа процесс не выходит,
  висит worker-server до внешнего kill. Первый боевой запрос (post #778
  в [topic 392](https://discuss.example.com/t/karernyj-trek/392/3))
  это сразу подтвердил - наблюдали зависание на 3+ минуты, что в
  параллели опередил Discourse-AI бот.
- **Решение:** Перевели runner на `--output-format stream-json` (одна JSON-
  строка на событие). Парсим stdout по строкам и убиваем процесс целиком
  через `os.killpg(SIGTERM -> SIGKILL)`, как только видим финальный
  объект `{"type":"result","result":...}`. Параллельно стрим stderr.
  Жёсткий потолок `AGENT_TIMEOUT_SEC=180` (с 600).
- **Последствия:**
  - Ответ улетает в Discourse сразу после готовности, не ждём фантомный
    процесс. Очередь не залипает.
  - Получаем `usage` (input/output/cache токены) и `is_error` в логах.
  - При обрыве стрима есть fallback - последнее сообщение
    `type==assistant`.
  - `start_new_session=True` нужно, чтобы убивать всё дочернее дерево
    (cursor-agent поднимает worker-server как отдельный node-процесс).

## ADR-005: Один воркер с `asyncio.Lock`

- **Контекст:** `cursor-agent --force --trust` имеет право писать в
  workspace и запускать shell. Параллельные запуски в одном workspace
  -> гонки на файлах, MCP-серверах, кеше Cursor.
- **Решение:** В runner глобальный `asyncio.Lock`, FIFO-обработка.
  Webhook отвечает Discourse сразу `200 OK queued`, чтобы не словить
  ретраи; реальная работа идёт в `asyncio.create_task`.
- **Последствия:**
  - В пиках запросы выстраиваются. На рабочую нагрузку 1-2 человек этого
    хватает; если когда-то понадобится больше - отдельные worktrees,
    но не сейчас.
  - Discourse не повторяет webhook -> не нужна явная idempotency на стороне
    приёма (но дедуп `state.json` всё равно есть от случайных ретраев).

## ADR-006: HMAC + allowlist + дедуп - единственная защита

- **Контекст:** Бридж торчит в интернет, агент работает с `--force --trust`.
  Любая дыра -> кто угодно запускает shell на твоём ПК.
- **Решение:**
  1. Discourse подписывает каждый webhook `X-Discourse-Event-Signature:
     sha256=...` тем же секретом, что в `WEBHOOK_SECRET`. Без подписи или
     с неверной - 401, в логах `rejected webhook: bad signature`.
  2. `ALLOWED_USERNAMES` ограничивает, чьи упоминания вообще запускают
     агента. Сейчас: только `admin`.
  3. `state.json` хранит уже обработанные `post_id` (deque на 5000
     последних), защита от ретраев и от пересечения нашего ответа с
     повторным `post_edited`.
  4. Лимит `MAX_POST_BYTES=8192` на длину `raw` в промпте.
  5. Структурированный лог `logs/<date>.jsonl` для всех решений (skip /
     agent_done / replied / error) - удобно потом разбирать.
- **Последствия:** Достаточно для домашнего использования. Если когда-то
  захочется пускать чужих - переключить на whitelist'ную PM с явным
  заявочным процессом.

## ADR-007: Бридж - Python + FastAPI, под `systemd --user`

- **Контекст:** Нужен лёгкий HTTP-сервер с асинхронным httpx и подпроцессами,
  легко поддерживаемый.
- **Решение:** Python 3.13 + FastAPI + uvicorn + httpx + python-dotenv.
  `.venv` в `bridge/discourse-cursor-bridge/.venv/`. Запуск через
  `systemctl --user` (две unit-файла - бридж и тоннель cloudflared).
  Симлинки в `~/.config/systemd/user/`, `loginctl enable-linger $USER` чтобы
  пережить logout.
- **Последствия:**
  - Никакого root, всё под обычным пользователем.
  - Перезапуск/логи штатные: `systemctl --user restart`,
    `journalctl --user -u ...`.
  - Туннель - отдельный сервис, можно гасить независимо.

## ADR-007: Отдельный sandbox-workspace для OpenAI-совместимого эндпоинта

- **Дата:** 2026-04-19
- **Контекст:** Появился внешний клиент (`tgzh`-бот, `https://github.com/vramdibs/tgzh`),
  которому нужно ходить в `cursor-agent` как в обычную OpenAI-совместимую
  LLM (fallback на случай отказа основного vLLM). Использовать тот же
  `WORKSPACE=~/workspace` нельзя: внешние запросы получили бы доступ к
  рабочему репо и к настройкам Discourse-флоу через тот же `cursor-agent`
  с `--force --trust`.
- **Решение:**
  - Новый эндпоинт `POST /v1/chat/completions` ([openai_endpoint.py](openai_endpoint.py))
    собирает свой `AgentRunner` с workspace `CURSOR_OPENAI_WORKSPACE`
    (по умолчанию `/tmp/cursor-openai-sandbox`, пустой каталог без `.cursor/`).
  - Авторизация — bearer-токен `BRIDGE_OPENAI_API_KEY`. Если ключ пуст —
    эндпоинт отвечает `503` (выключен).
  - Картинки в `image_url` принимаются только как `data:image/...;base64,...`;
    каждая пишется во временный файл внутри sandbox и удаляется после ответа,
    путь подставляется в собранный prompt — это та же конвенция, что у
    `cursor_cli_client.run_cursor_agent` в проекте `tgzh`.
  - Discourse-flow `--force --trust` оставлен как есть — он на отдельном
    `WORKSPACE` и отдельном маршруте, эта правка его не затрагивает.
- **Последствия:**
  - Внешний клиент не видит ни `~/workspace`, ни секреты Discourse.
  - `cursor-agent` всё ещё может использовать инструменты (tools/MCP),
    поэтому `BRIDGE_OPENAI_API_KEY` нужно хранить как обычный API-ключ
    и не выдавать недоверенным сервисам.
  - Sandbox должен оставаться пустым: периодическая чистка
    `CURSOR_OPENAI_WORKSPACE/.openai-images/` — необязательна, файлы
    удаляются в `finally`, но процесс может умереть до cleanup'а.

## ADR-008: `body.model` уважается, но через жёсткий allowlist

- **Дата:** 2026-04-20
- **Контекст:** Telegram-бот `tgzh` (`https://github.com/vramdibs/tgzh`) шлёт OpenAI-запросы
  с полем `model` (например, `composer-2`), ожидая, что мост запустит
  `cursor-agent --model composer-2`. До этого ADR `make_router` дёргал
  `runner_factory()` без аргумента, и `AgentRunner.model` фиксировался на
  старте процесса значением `CURSOR_MODEL`. Поле `model` запроса
  игнорировалось — какую бы модель ни просил клиент, он получал ту, что
  в `.env` бриджа.
- **Решение:**
  1. `runner_factory` теперь принимает kw `model: str | None`. `None` =
     «оставить дефолт из `Config.cursor_model`». Сервер строит новый
     `AgentRunner` на каждый запрос с нужной моделью.
  2. `body.model` валидируется по allowlist (`BRIDGE_OPENAI_ALLOWED_MODELS`,
     CSV; пусто или `*` = дефолтный набор Cursor CLI: `auto`, `composer-2`,
     `gpt-5`, `gpt-5-codex`, `claude-sonnet-4`, `claude-sonnet-4-thinking`).
     Любая другая строка → `400`. Без этого клиент мог бы инжектить
     произвольное значение в CLI-флаг `--model`.
  3. Пустая строка / `default` в `body.model` = взять `CURSOR_MODEL` (т.е.
     старое поведение). Это сохраняет совместимость для клиентов, которым
     модель не важна.
  4. `make_router` обрабатывает `TypeError` при вызове старой
     бесаргументной фабрики, чтобы внешние интеграции не сломались.
- **Последствия:**
  - Каждый OpenAI-запрос теперь стартует свой `AgentRunner` с собственным
    `asyncio.Lock` — конкурентные запросы больше не сериализуются на этом
    Lock'е (общий лимит на хост держат bearer-token + лимит длины prompt).
    Discourse-flow по-прежнему использует один shared `agent`-инстанс с
    единым Lock'ом.
  - Список разрешённых моделей надо синхронно обновлять с тем, что умеет
    локальная установка `cursor-agent`. При апгрейде CLI — заглянуть в
    `cursor-agent --help` и при необходимости расширить
    `DEFAULT_ALLOWED_CURSOR_MODELS` или задать `BRIDGE_OPENAI_ALLOWED_MODELS`
    в `.env`.

## История событий

- **2026-04-17 22:08** - первый запуск моста, `BOT_USERNAME=qwen_bot`,
  `AGENT_TIMEOUT_SEC=600`, `--output-format json`.
- **2026-04-17 22:18** - открыт `BRIDGE_HOST=0.0.0.0`, поднят туннель,
  webhook прокинут до `http://cursor-bridge.example.com/discourse/webhook`.
- **2026-04-17 22:19** - первое боевое сообщение в
  [topic 392 post #2](https://discuss.example.com/t/karernyj-trek/392/2):
  `@Qwen_bot какую модель ты используешь?`. Мост принял, запустил
  `cursor-agent`, тот завис. Параллельный Discourse-AI бот опередил его на
  ответе #3.
- **2026-04-17 22:28** - применены ADR-003 и ADR-004:
  - создан `cursor_bot` (id=23) + API key, добавлен в группу `Bot`,
  - переведено на `stream-json` + ранний exit + `os.killpg`,
  - таймаут 180с,
  - smoke зелёный.
