"""Discourse -> Cursor agent bridge.

Receives Discourse webhooks (post_created/post_edited), filters posts that
@mention the configured bot user, runs `cursor-agent` headlessly with the
topic context as prompt, and posts the agent's reply back to the same topic.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

# Под systemd `.env` рядом с проектом не подключается через EnvironmentFile,
# а `OpenAIEndpointConfig.from_env()` читает только `os.environ`. Прокидываем
# `.env` в окружение тут (override=False, чтобы реальные системные env побеждали).
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from agent_runner import AgentRunner  # noqa: E402  - после load_dotenv
from config import Config, load_config  # noqa: E402
from discourse_client import DiscourseClient, TopicSnapshot  # noqa: E402
from openai_endpoint import OpenAIEndpointConfig, make_router as make_openai_router  # noqa: E402
from prompt import build_prompt  # noqa: E402
from state import StateStore  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bridge")


config: Config = load_config()
discourse = DiscourseClient(config.discourse)
agent = AgentRunner(
    binary=config.cursor_agent_bin,
    workspace=config.workspace,
    timeout_sec=config.agent_timeout_sec,
    model=config.cursor_model,
)
state = StateStore(config.state_file)

app = FastAPI(title="discourse-cursor-bridge")


def _openai_runner_factory(*, model: str | None = None) -> AgentRunner:
    """Отдельный AgentRunner для OpenAI-эндпоинта: пустой sandbox-workspace,
    тот же бинарник, своя длительность таймаута. Не делим Lock с Discourse-flow,
    но оба пишут в один и тот же cursor-agent на хосте — нагрузку контролирует
    bearer-token + лимит длины prompt.

    `model` — slug, провалидированный `_resolve_requested_model` поверх
    `BRIDGE_OPENAI_ALLOWED_MODELS`. `None` означает «используй модель из
    основного Config.cursor_model» (старое поведение).
    """
    cfg_oai = OpenAIEndpointConfig.from_env()
    timeout_raw = (os.getenv("BRIDGE_OPENAI_TIMEOUT_SEC") or "").strip()
    try:
        timeout = int(timeout_raw) if timeout_raw else config.agent_timeout_sec
    except ValueError:
        timeout = config.agent_timeout_sec
    chosen_model = (model or "").strip() or config.cursor_model
    return AgentRunner(
        binary=config.cursor_agent_bin,
        workspace=str(cfg_oai.sandbox_workspace),
        timeout_sec=timeout,
        model=chosen_model,
    )


app.include_router(make_openai_router(runner_factory=_openai_runner_factory))


_MENTION_RE_CACHE: dict[str, re.Pattern[str]] = {}


def mention_re(username: str) -> re.Pattern[str]:
    if username not in _MENTION_RE_CACHE:
        _MENTION_RE_CACHE[username] = re.compile(
            rf"(?:^|[^a-zA-Z0-9_])@{re.escape(username)}(?![a-zA-Z0-9_])",
            re.IGNORECASE,
        )
    return _MENTION_RE_CACHE[username]


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret:
        return True
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def _today_log_path() -> Path:
    return config.log_dir / f"{dt.date.today().isoformat()}.jsonl"


def _log_event(event: dict[str, Any]) -> None:
    event = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), **event}
    try:
        with _today_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("could not write log event: %s", exc)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "bot": config.bot_username,
        "workspace": config.workspace,
        "discourse": config.discourse.base_url,
        "allowed_users": config.allowed_usernames,
    }


@app.post("/discourse/webhook")
async def webhook(
    request: Request,
    x_discourse_event: str | None = Header(default=None),
    x_discourse_event_type: str | None = Header(default=None),
    x_discourse_event_signature: str | None = Header(default=None),
    x_discourse_instance: str | None = Header(default=None),
) -> JSONResponse:
    body = await request.body()

    if not verify_signature(config.webhook_secret, body, x_discourse_event_signature):
        log.warning("rejected webhook: bad signature (event=%s)", x_discourse_event)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")

    if x_discourse_event_type != "post" or x_discourse_event not in {"post_created", "post_edited"}:
        return JSONResponse({"ignored": True, "reason": f"event {x_discourse_event_type}/{x_discourse_event}"})

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"bad json: {exc}") from exc

    post = payload.get("post") or {}
    decision = _should_react(post)
    if decision is not None:
        _log_event({"kind": "skip", "reason": decision, "post_id": post.get("id"), "user": post.get("username")})
        return JSONResponse({"ignored": True, "reason": decision})

    asyncio.create_task(_handle_post(post, x_discourse_instance or ""))
    return JSONResponse({"queued": True, "post_id": post.get("id")})


def _should_react(post: dict[str, Any]) -> str | None:
    if not post:
        return "no_post_field"
    post_id = post.get("id")
    if not isinstance(post_id, int):
        return "no_post_id"
    if state.has(post_id):
        return "duplicate"
    username = str(post.get("username") or "")
    if not username:
        return "no_username"
    if username == config.bot_username:
        return "self_post"
    if config.allowed_usernames and username not in config.allowed_usernames:
        return f"user_not_allowed:{username}"
    raw = str(post.get("raw") or "")
    if not raw:
        return "empty_raw"
    if not mention_re(config.bot_username).search(raw):
        return "no_mention"
    return None


async def _handle_post(post: dict[str, Any], instance: str) -> None:
    post_id = int(post["id"])
    topic_id = int(post["topic_id"])
    post_number = int(post.get("post_number") or 0)
    username = str(post.get("username") or "")
    raw_excerpt = str(post.get("raw") or "")[:200]

    await state.remember(post_id)
    log.info(
        "handling post id=%d topic=%d user=@%s post_number=%d (instance=%s)",
        post_id, topic_id, username, post_number, instance,
    )

    try:
        snapshot = await discourse.get_topic(topic_id, last_n=config.context_posts)
    except Exception as exc:
        log.exception("failed to fetch topic %d", topic_id)
        _log_event({"kind": "error", "stage": "get_topic", "post_id": post_id, "error": str(exc)})
        return

    triggering = _ensure_triggering_post_present(snapshot, post)
    prompt = build_prompt(
        snapshot=triggering,
        triggering_post_id=post_id,
        triggering_username=username,
        bot_username=config.bot_username,
        max_post_bytes=config.max_post_bytes,
    )

    started = dt.datetime.now(dt.timezone.utc)
    try:
        result = await agent.run(prompt)
    except Exception as exc:
        log.exception("agent crashed for post %d", post_id)
        _log_event({"kind": "error", "stage": "agent", "post_id": post_id, "error": str(exc)})
        return

    duration = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
    _log_event({
        "kind": "agent_done",
        "post_id": post_id,
        "topic_id": topic_id,
        "user": username,
        "duration_sec": round(duration, 2),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "ok": result.ok,
        "stdout_len": len(result.raw_stdout),
        "stderr_tail": result.raw_stderr[-400:],
        "raw_excerpt": raw_excerpt,
    })

    if not result.ok or not result.text.strip():
        log.warning("agent did not produce a usable answer for post %d", post_id)
        return

    try:
        reply = await discourse.create_post(
            topic_id=topic_id,
            raw=result.text.strip(),
            reply_to_post_number=post_number or None,
        )
    except Exception as exc:
        log.exception("failed to post reply for post %d", post_id)
        _log_event({"kind": "error", "stage": "create_post", "post_id": post_id, "error": str(exc)})
        return

    reply_id = reply.get("id") if isinstance(reply, dict) else None
    if isinstance(reply_id, int):
        await state.remember(reply_id)
    _log_event({
        "kind": "replied",
        "post_id": post_id,
        "topic_id": topic_id,
        "reply_post_id": reply_id,
    })
    log.info("replied to post %d -> reply id=%s", post_id, reply_id)


def _ensure_triggering_post_present(snapshot: TopicSnapshot, post: dict[str, Any]) -> TopicSnapshot:
    """If the freshly created post has not propagated to the topic JSON yet,
    splice it in so the prompt sees the actual trigger text."""
    post_id = int(post["id"])
    if any(p.id == post_id for p in snapshot.posts):
        return snapshot
    from discourse_client import TopicPost
    extra = TopicPost(
        id=post_id,
        post_number=int(post.get("post_number") or (snapshot.posts[-1].post_number + 1 if snapshot.posts else 1)),
        username=str(post.get("username") or ""),
        raw=str(post.get("raw") or ""),
    )
    return TopicSnapshot(
        id=snapshot.id,
        title=snapshot.title,
        category_id=snapshot.category_id,
        posts=[*snapshot.posts, extra],
    )


def main() -> None:
    log.info(
        "starting bridge on %s:%d (bot=@%s, workspace=%s, allowed=%s)",
        config.host, config.port, config.bot_username, config.workspace,
        config.allowed_usernames or "ANY",
    )
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
