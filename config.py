"""Runtime config loaded from .env (bridge) and discourse.env (existing)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


HERE = Path(__file__).resolve().parent


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class DiscourseAuth:
    base_url: str
    api_key: str
    username: str


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    webhook_secret: str
    bot_username: str
    allowed_usernames: list[str]
    cursor_agent_bin: str
    workspace: str
    cursor_model: str
    agent_timeout_sec: int
    context_posts: int
    max_post_bytes: int
    state_file: Path
    log_dir: Path
    discourse: DiscourseAuth = field(repr=False)


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def load_config() -> Config:
    bridge_env = _load_env_file(HERE / ".env")
    env = {**bridge_env, **os.environ}

    discourse_env_path = Path(env.get("DISCOURSE_ENV_FILE", "~/workspace/discourse.env"))
    discourse_env = _load_env_file(discourse_env_path)

    base_url = discourse_env.get("DISCOURSE_URL")
    # Prefer the dedicated cursor_bot credentials when present; fall back to the
    # global qwen_bot ones so the bridge still works on instances that have not
    # been migrated yet.
    api_key = discourse_env.get("DISCOURSE_API_KEY_CURSOR") or discourse_env.get("DISCOURSE_API_KEY")
    username = discourse_env.get("DISCOURSE_USERNAME_CURSOR") or discourse_env.get("DISCOURSE_USERNAME")
    if not (base_url and api_key and username):
        raise RuntimeError(
            f"discourse env file {discourse_env_path} must define "
            "DISCOURSE_URL plus DISCOURSE_API_KEY[_CURSOR] and DISCOURSE_USERNAME[_CURSOR]"
        )

    state_file = Path(env.get("STATE_FILE", str(HERE / "state.json")))
    log_dir = Path(env.get("LOG_DIR", str(HERE / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        host=env.get("BRIDGE_HOST", "127.0.0.1"),
        port=int(env.get("BRIDGE_PORT", "8787")),
        webhook_secret=env.get("WEBHOOK_SECRET", ""),
        bot_username=env.get("BOT_USERNAME", username),
        allowed_usernames=_split_csv(env.get("ALLOWED_USERNAMES")),
        cursor_agent_bin=env.get("CURSOR_AGENT_BIN", "~/.local/bin/cursor-agent"),
        workspace=env.get("WORKSPACE", "~/workspace"),
        cursor_model=env.get("CURSOR_MODEL", "").strip(),
        agent_timeout_sec=int(env.get("AGENT_TIMEOUT_SEC", "600")),
        context_posts=int(env.get("CONTEXT_POSTS", "10")),
        max_post_bytes=int(env.get("MAX_POST_BYTES", "8192")),
        state_file=state_file,
        log_dir=log_dir,
        discourse=DiscourseAuth(base_url=base_url.rstrip("/"), api_key=api_key, username=username),
    )
