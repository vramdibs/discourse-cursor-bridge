"""Tiny on-disk dedup store for already-processed post IDs."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from pathlib import Path


log = logging.getLogger(__name__)

MAX_REMEMBERED = 5000


class StateStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()
        self._seen: deque[int] = deque(maxlen=MAX_REMEMBERED)
        self._seen_set: set[int] = set()
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not load state from %s: %s", self._path, exc)
            return
        ids = data.get("seen_post_ids") if isinstance(data, dict) else None
        if isinstance(ids, list):
            for pid in ids[-MAX_REMEMBERED:]:
                try:
                    pid_int = int(pid)
                except (TypeError, ValueError):
                    continue
                self._seen.append(pid_int)
                self._seen_set.add(pid_int)

    def has(self, post_id: int) -> bool:
        return post_id in self._seen_set

    async def remember(self, post_id: int) -> None:
        async with self._lock:
            if post_id in self._seen_set:
                return
            if len(self._seen) == self._seen.maxlen:
                evicted = self._seen[0]
                self._seen_set.discard(evicted)
            self._seen.append(post_id)
            self._seen_set.add(post_id)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"seen_post_ids": list(self._seen)}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)
