"""Общие фикстуры для тестов bridge.

Главное — изолировать тесты от реального `.env` рядом с проектом и от
`~/workspace/discourse.env`, иначе CI/локалка будет видеть реальные
ключи и может случайно подёргать настоящий cursor-agent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

# Добавляем корень проекта в sys.path, чтобы тесты могли импортировать
# `openai_endpoint` без установки пакета.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """По умолчанию очищаем все «опасные» env-переменные. Конкретные тесты
    переопределяют их через `monkeypatch.setenv`."""
    for key in (
        "BRIDGE_OPENAI_API_KEY",
        "CURSOR_OPENAI_WORKSPACE",
        "BRIDGE_OPENAI_TIMEOUT_SEC",
        "BRIDGE_OPENAI_MAX_PROMPT_CHARS",
        "BRIDGE_OPENAI_ALLOWED_MODELS",
    ):
        monkeypatch.delenv(key, raising=False)
