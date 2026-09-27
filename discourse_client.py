"""Tiny Discourse REST wrapper used by the bridge.

Only what the bridge actually needs: read a topic with raw posts, create a post.
Honors the publishing rules from .cursor/rules/discourse-publish.mdc:
- Content-Type: application/json (not multipart) for /posts.json
- Inject the TOC blob at the top of each post body
- Backoff on HTTP 429
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from config import DiscourseAuth


log = logging.getLogger(__name__)


TOC_BLOB = '<div data-theme-toc="true"> </div>\n\n'


@dataclass(frozen=True)
class TopicPost:
    id: int
    post_number: int
    username: str
    raw: str


@dataclass(frozen=True)
class TopicSnapshot:
    id: int
    title: str
    category_id: int | None
    posts: list[TopicPost]


class DiscourseClient:
    def __init__(self, auth: DiscourseAuth, timeout: float = 30.0):
        self._auth = auth
        self._headers = {
            "Api-Key": auth.api_key,
            "Api-Username": auth.username,
            "Accept": "application/json",
        }
        self._timeout = timeout

    async def get_topic(self, topic_id: int, last_n: int = 10) -> TopicSnapshot:
        """Fetch a topic with raw post bodies. Returns the last `last_n` posts."""
        url = f"{self._auth.base_url}/t/{topic_id}.json"
        params = {"include_raw": "true", "print": "true"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=self._headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        post_stream = (data.get("post_stream") or {}).get("posts") or []
        posts: list[TopicPost] = []
        for raw_post in post_stream:
            posts.append(
                TopicPost(
                    id=int(raw_post["id"]),
                    post_number=int(raw_post["post_number"]),
                    username=str(raw_post.get("username", "")),
                    raw=str(raw_post.get("raw", "") or raw_post.get("cooked", "")),
                )
            )
        if last_n > 0:
            posts = posts[-last_n:]
        return TopicSnapshot(
            id=int(data["id"]),
            title=str(data.get("title", "")),
            category_id=data.get("category_id"),
            posts=posts,
        )

    async def create_post(
        self,
        topic_id: int,
        raw: str,
        reply_to_post_number: int | None = None,
        edit_reason: str | None = None,
    ) -> dict[str, Any]:
        """Create a reply post in `topic_id`. Auto-prepends the TOC blob."""
        url = f"{self._auth.base_url}/posts.json"
        body: dict[str, Any] = {
            "topic_id": topic_id,
            "raw": TOC_BLOB + raw if not raw.startswith(TOC_BLOB) else raw,
        }
        if reply_to_post_number is not None:
            body["reply_to_post_number"] = reply_to_post_number
        if edit_reason:
            body["edit_reason"] = edit_reason

        headers = {
            **self._headers,
            "Content-Type": "application/json",
        }
        return await self._post_json_with_retry(url, headers, body)

    async def _post_json_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        attempt = 0
        backoff = 27.0
        last_exc: Exception | None = None
        while attempt < max_attempts:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, headers=headers, json=body)
                if resp.status_code == 429:
                    log.warning("discourse 429 on attempt %d, sleeping %.0fs", attempt, backoff)
                    await asyncio.sleep(backoff)
                    backoff *= 1.5
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code in (502, 503, 504):
                    await asyncio.sleep(min(backoff, 30))
                    backoff *= 1.5
                    continue
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(min(backoff, 30))
                backoff *= 1.5
        assert last_exc is not None
        raise last_exc
