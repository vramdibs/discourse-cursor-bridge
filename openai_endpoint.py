"""OpenAI-compatible /v1/chat/completions поверх AgentRunner (cursor-agent).

Эндпоинт нужен, чтобы внешние клиенты, умеющие говорить с
OpenAI/vLLM-совместимым API (например, Telegram-бот tgzh, см.
`https://github.com/vramdibs/tgzh`), могли использовать `cursor-agent` как
fallback-LLM при отказе основной модели.

Особенности и ограничения:

- `cursor-agent` запускается subprocess'ом в отдельном "sandbox"-workspace
  (env `CURSOR_OPENAI_WORKSPACE`), отдельно от Discourse-flow с `--force --trust`.
- `messages` склеиваются в один prompt с маркерами `[system]/[user]/[assistant]`,
  поскольку у `cursor-agent` нет ролей. Картинки в `image_url` декодируются
  во временные файлы, путь к ним вписывается в prompt — точно так же, как это
  делает `cursor_cli_client.run_cursor_agent` в проекте tgzh.
- `stream=true` пока не поддержан (у cursor-agent другая семантика стрима);
  вернётся `400`.
- Авторизация — `Authorization: Bearer <BRIDGE_OPENAI_API_KEY>`. Если ключ
  не задан в env, эндпоинт возвращает `503` (выключен).
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from agent_runner import AgentRunner, AgentResult


log = logging.getLogger("bridge.openai")


# -- Pydantic-схемы запроса/ответа --

class TextPart(BaseModel):
    type: Literal["text"]
    text: str = Field(..., max_length=200_000)


class ImageUrlInner(BaseModel):
    url: str = Field(..., max_length=20_000_000)
    detail: str | None = None


class ImagePart(BaseModel):
    type: Literal["image_url"]
    image_url: ImageUrlInner


ContentPart = TextPart | ImagePart


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart]
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=64)
    max_tokens: int | None = Field(default=None, ge=1, le=131_072)
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    user: str | None = None


class ChoiceMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: Literal["stop", "length", "content_filter"] = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


# -- Конфигурация эндпоинта --


# Слаги, которые на момент 04.2026 принимает `cursor-agent --model` (см.
# `cursor-agent --help`). Список используется как allowlist для поля `model`
# OpenAI-запроса, чтобы клиент не просочил произвольную строку в CLI-флаг.
# Если конкретный bridge хочет жёстче — задайте `BRIDGE_OPENAI_ALLOWED_MODELS`
# в env (CSV); пустая строка = разрешён только дефолт `cursor_model` из Config
# (сохраняем старое поведение «модель из env, body.model игнорируется»).
DEFAULT_ALLOWED_CURSOR_MODELS: tuple[str, ...] = (
    "auto",
    "composer-2",
    "composer-2.5",
    "cursor-grok-4.6-low",
    "gpt-5",
    "gpt-5-codex",
    "claude-sonnet-4",
    "claude-sonnet-4-thinking",
)


@dataclass(frozen=True)
class OpenAIEndpointConfig:
    """Параметры эндпоинта читаются из env при сборке маршрута, чтобы не
    тащить их через основной Config (Discourse-flow на них не завязан)."""

    api_key: str
    sandbox_workspace: Path
    max_prompt_chars: int
    image_tmp_dir: Path
    # Пустой набор = поле `model` запроса игнорируется, всегда используется
    # модель из основного `Config.cursor_model` (`runner_factory(model=None)`).
    # Непустой = разрешён только этот список плюс пустая строка (= дефолт).
    allowed_models: frozenset[str]

    @classmethod
    def from_env(cls) -> "OpenAIEndpointConfig":
        api_key = (os.getenv("BRIDGE_OPENAI_API_KEY") or "").strip()
        ws_raw = (os.getenv("CURSOR_OPENAI_WORKSPACE") or "/tmp/cursor-openai-sandbox").strip()
        max_chars_raw = (os.getenv("BRIDGE_OPENAI_MAX_PROMPT_CHARS") or "200000").strip()
        try:
            max_chars = int(max_chars_raw)
        except ValueError:
            max_chars = 200_000
        max_chars = max(1_000, min(2_000_000, max_chars))

        sandbox = Path(ws_raw)
        try:
            sandbox.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # не падаем — отдадим 503 при первом запросе
            log.warning("cannot ensure sandbox workspace %s: %s", sandbox, exc)

        img_dir = sandbox / ".openai-images"
        try:
            img_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            img_dir = Path(tempfile.gettempdir())

        # `BRIDGE_OPENAI_ALLOWED_MODELS=*` = разрешить весь дефолтный список.
        # CSV = ровно эти. Незаданная переменная = дефолтный список (включаем
        # маппинг по умолчанию, иначе bridge не пропустит ни одного слага).
        allowed_raw = (os.getenv("BRIDGE_OPENAI_ALLOWED_MODELS") or "").strip()
        if allowed_raw == "":
            allowed = frozenset(DEFAULT_ALLOWED_CURSOR_MODELS)
        elif allowed_raw == "*":
            allowed = frozenset(DEFAULT_ALLOWED_CURSOR_MODELS)
        else:
            allowed = frozenset(
                item.strip() for item in allowed_raw.split(",") if item.strip()
            )

        return cls(
            api_key=api_key,
            sandbox_workspace=sandbox,
            max_prompt_chars=max_chars,
            image_tmp_dir=img_dir,
            allowed_models=allowed,
        )


# -- Хелперы --

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/(?:jpeg|jpg|png|webp|gif));base64,(?P<b64>.+)$",
    re.IGNORECASE | re.DOTALL,
)

_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_ROLE_LABEL: dict[str, str] = {
    "system": "[system]",
    "user": "[user]",
    "assistant": "[assistant]",
    "tool": "[tool]",
}


def _decode_data_url(url: str, *, dest_dir: Path) -> Path | None:
    """Сохранить data:image/...;base64,... во временный файл и вернуть путь.

    Поддержка только image/* — все остальные схемы игнорируются (для
    cursor-agent передавать произвольные http(s)-ссылки бессмысленно: он
    их сам не скачает в текущей конфигурации).
    """
    m = _DATA_URL_RE.match(url.strip())
    if not m:
        return None
    mime = m.group("mime").lower()
    ext = _EXT_BY_MIME.get(mime, ".bin")
    try:
        raw = base64.b64decode(m.group("b64"), validate=True)
    except (binascii.Error, ValueError) as exc:
        log.warning("bad base64 in image_url: %s", exc)
        return None
    if not raw:
        return None
    fd, path = tempfile.mkstemp(prefix="cursor-img-", suffix=ext, dir=str(dest_dir))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    except OSError as exc:
        log.warning("cannot write tmp image %s: %s", path, exc)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    return Path(path)


def build_prompt_and_images(
    messages: list[ChatMessage],
    *,
    image_tmp_dir: Path,
    max_chars: int,
) -> tuple[str, list[Path]]:
    """Собирает один текстовый prompt из messages и список путей к временным
    картинкам. Картинки нужно удалить после вызова (caller отвечает)."""

    out_lines: list[str] = []
    images: list[Path] = []
    for msg in messages:
        label = _ROLE_LABEL.get(msg.role, f"[{msg.role}]")
        if isinstance(msg.content, str):
            out_lines.append(f"{label}\n{msg.content.strip()}")
            continue
        text_parts: list[str] = []
        for part in msg.content:
            if isinstance(part, TextPart):
                text_parts.append(part.text)
            elif isinstance(part, ImagePart):
                p = _decode_data_url(part.image_url.url, dest_dir=image_tmp_dir)
                if p is not None:
                    images.append(p)
                    text_parts.append(
                        f"Analyze the image at this absolute path "
                        f"(read it via your tools): {p}"
                    )
                else:
                    text_parts.append(
                        "[image was provided as non-data URL or failed to decode; ignored]"
                    )
        joined = "\n\n".join(t for t in (part.strip() for part in text_parts) if t)
        if joined:
            out_lines.append(f"{label}\n{joined}")

    prompt = "\n\n".join(out_lines).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="empty prompt after assembly")
    if len(prompt) > max_chars:
        raise HTTPException(
            status_code=413,
            detail=f"assembled prompt is longer than {max_chars} chars",
        )
    return prompt, images


def _cleanup_images(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not unlink tmp image %s: %s", p, exc)


def _result_to_response(model: str, result: AgentResult) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
        created=int(time.time()),
        model=model,
        choices=[Choice(message=ChoiceMessage(content=result.text))],
        usage=Usage(),
    )


# -- Зависимости и роутер --


def _config_dependency() -> OpenAIEndpointConfig:
    return OpenAIEndpointConfig.from_env()


def _verify_bearer(
    authorization: str | None = Header(default=None),
    cfg: OpenAIEndpointConfig = Depends(_config_dependency),
) -> OpenAIEndpointConfig:
    if not cfg.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="openai-compat endpoint disabled (BRIDGE_OPENAI_API_KEY is empty)",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = authorization.split(" ", 1)[1].strip()
    # constant-time compare; hmac.compare_digest имеет правильные семантики
    import hmac as _hmac
    if not _hmac.compare_digest(token.encode("utf-8"), cfg.api_key.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad bearer token",
        )
    return cfg


def _resolve_requested_model(
    requested: str,
    allowed: frozenset[str],
) -> str | None:
    """Нормализует `body.model` в slug для `cursor-agent --model`.

    - Пустая строка / `default` / `auto`-без-allowlist → `None` (использовать
      дефолтную модель из основного Config).
    - Иначе — slug должен быть в `allowed`. Если нет — 400, иначе кто угодно
      смог бы инжектить произвольную строку в CLI-флаг.
    """
    name = (requested or "").strip()
    if name == "" or name.lower() == "default":
        return None
    if name not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"model {name!r} is not in BRIDGE_OPENAI_ALLOWED_MODELS; "
                f"allowed: {sorted(allowed)}"
            ),
        )
    return name


def make_router(*, runner_factory) -> APIRouter:
    """`runner_factory` — callable `(model: str | None) -> AgentRunner`. Вынесен
    отдельно, чтобы `server.py` мог построить отдельный `AgentRunner` с
    sandbox-workspace и подменить slug, а тесты — подсунуть фейк. Допустимы
    обе сигнатуры — с аргументом `model` и без — для обратной совместимости
    со старыми фабриками (мы пробуем сначала с аргументом, при `TypeError`
    падаем обратно на бесаргументный вариант)."""

    router = APIRouter()

    @router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
    async def chat_completions(
        body: ChatCompletionRequest,
        cfg: OpenAIEndpointConfig = Depends(_verify_bearer),
    ) -> Any:
        if body.stream:
            raise HTTPException(status_code=400, detail="stream=true is not supported")

        resolved_model = _resolve_requested_model(body.model, cfg.allowed_models)

        prompt, images = build_prompt_and_images(
            body.messages,
            image_tmp_dir=cfg.image_tmp_dir,
            max_chars=cfg.max_prompt_chars,
        )

        try:
            try:
                runner: AgentRunner = runner_factory(model=resolved_model)
            except TypeError:
                # Старые фабрики не принимают `model` (legacy-тесты, чужие
                # внедрения) — оставляем поведение «модель берёт сам runner».
                runner = runner_factory()
            log.info(
                "openai chat: requested_model=%s resolved_model=%s msgs=%d prompt_chars=%d images=%d",
                body.model, resolved_model or "<default>",
                len(body.messages), len(prompt), len(images),
            )
            result = await runner.run(prompt)
        finally:
            _cleanup_images(images)

        if result.timed_out:
            raise HTTPException(status_code=502, detail="cursor-agent timed out")
        if not result.ok or not (result.text or "").strip():
            raise HTTPException(
                status_code=502,
                detail=(
                    "cursor-agent did not produce a usable answer "
                    f"(exit_code={result.exit_code}, is_error={result.is_error})"
                ),
            )

        return _result_to_response(body.model, result)

    return router
