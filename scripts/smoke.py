"""End-to-end smoke test that exercises the pipeline without touching Discourse.

Stubs the Discourse client (no HTTP), wires up a fake `cursor-agent` binary
(scripts/fake-cursor-agent.sh) and fires a synthetic post_created webhook
through the FastAPI app via httpx.ASGITransport.

Run from the project root:
    .venv/bin/python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

os.environ.setdefault("DISCOURSE_ENV_FILE", str(HERE.parent.parent / "discourse.env"))
os.environ["WEBHOOK_SECRET"] = "smoke-secret"
os.environ["ALLOWED_USERNAMES"] = "admin"
os.environ["BOT_USERNAME"] = "qwen_bot"
os.environ["CURSOR_AGENT_BIN"] = str(HERE / "scripts" / "fake-cursor-agent.sh")
os.environ["AGENT_TIMEOUT_SEC"] = "30"
os.environ["STATE_FILE"] = str(HERE / "state-smoke.json")
os.environ["LOG_DIR"] = str(HERE / "logs")
state_file = Path(os.environ["STATE_FILE"])
if state_file.exists():
    state_file.unlink()

import httpx

import server
from discourse_client import TopicPost, TopicSnapshot


async def fake_get_topic(topic_id: int, last_n: int = 10) -> TopicSnapshot:
    return TopicSnapshot(
        id=topic_id,
        title="Smoke topic",
        category_id=6,
        posts=[
            TopicPost(id=111, post_number=1, username="admin", raw="Открываю топик."),
            TopicPost(id=112, post_number=2, username="qwen_bot", raw="Привет."),
        ],
    )


captured_posts: list[dict] = []


async def fake_create_post(topic_id: int, raw: str, reply_to_post_number=None, edit_reason=None):
    captured_posts.append(
        {"topic_id": topic_id, "raw": raw, "reply_to_post_number": reply_to_post_number}
    )
    return {"id": 99999, "topic_id": topic_id}


server.discourse.get_topic = fake_get_topic           # type: ignore[assignment]
server.discourse.create_post = fake_create_post       # type: ignore[assignment]


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def fire(post: dict, *, signed: bool = True) -> httpx.Response:
    body = json.dumps({"post": post}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Discourse-Event-Type": "post",
        "X-Discourse-Event": "post_created",
    }
    if signed:
        headers["X-Discourse-Event-Signature"] = sign("smoke-secret", body)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/discourse/webhook", content=body, headers=headers)


async def main() -> int:
    failures: list[str] = []

    health_transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=health_transport, base_url="http://test") as client:
        h = await client.get("/healthz")
    assert h.status_code == 200, h.text
    print("[OK] /healthz ->", h.json())

    bad_sig = await fire({"id": 1, "topic_id": 1, "post_number": 3, "username": "admin", "raw": "@qwen_bot ping"}, signed=False)
    if bad_sig.status_code != 401:
        failures.append(f"unsigned webhook should be 401, got {bad_sig.status_code}")
    else:
        print("[OK] unsigned webhook rejected with 401")

    skip_self = await fire({"id": 2, "topic_id": 1, "post_number": 4, "username": "qwen_bot", "raw": "@qwen_bot loop?"})
    if skip_self.status_code != 200 or not skip_self.json().get("ignored"):
        failures.append(f"self-post should be ignored: {skip_self.status_code} {skip_self.text}")
    else:
        print("[OK] self-post ignored:", skip_self.json())

    skip_user = await fire({"id": 3, "topic_id": 1, "post_number": 5, "username": "stranger", "raw": "@qwen_bot do evil"})
    if skip_user.status_code != 200 or "user_not_allowed" not in skip_user.json().get("reason", ""):
        failures.append(f"stranger should be ignored: {skip_user.text}")
    else:
        print("[OK] non-allowed user ignored:", skip_user.json())

    skip_no_mention = await fire({"id": 4, "topic_id": 1, "post_number": 6, "username": "admin", "raw": "no mention here"})
    if skip_no_mention.status_code != 200 or skip_no_mention.json().get("reason") != "no_mention":
        failures.append(f"no-mention should be ignored: {skip_no_mention.text}")
    else:
        print("[OK] no-mention ignored")

    real = await fire({
        "id": 5, "topic_id": 42, "post_number": 7,
        "username": "admin",
        "raw": "Эй @qwen_bot, ответь pong",
    })
    if real.status_code != 200 or not real.json().get("queued"):
        failures.append(f"real call should be queued: {real.text}")
    else:
        print("[OK] real call queued:", real.json())

    for _ in range(50):
        await asyncio.sleep(0.2)
        if captured_posts:
            break

    if not captured_posts:
        failures.append("no reply was posted (agent or pipeline did not complete)")
    else:
        reply = captured_posts[0]
        print("[OK] reply posted:", json.dumps(reply, ensure_ascii=False))
        if reply["topic_id"] != 42:
            failures.append(f"reply went to wrong topic: {reply['topic_id']}")
        if reply["reply_to_post_number"] != 7:
            failures.append(f"reply_to_post_number wrong: {reply['reply_to_post_number']}")
        if "smoke ok" not in reply["raw"]:
            failures.append(f"reply body unexpected: {reply['raw']!r}")

    dup = await fire({
        "id": 5, "topic_id": 42, "post_number": 7,
        "username": "admin",
        "raw": "Эй @qwen_bot, ответь pong",
    })
    if dup.status_code != 200 or dup.json().get("reason") != "duplicate":
        failures.append(f"duplicate should be ignored: {dup.text}")
    else:
        print("[OK] duplicate ignored")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
