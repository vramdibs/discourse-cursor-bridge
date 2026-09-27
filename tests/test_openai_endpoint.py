"""Юнит-тесты для openai_endpoint.

AgentRunner мокается — тесты не дёргают реальный cursor-agent. Проверяем:
- bearer-авторизацию (401, 503),
- сборку prompt из messages + base64-картинок,
- happy path с OpenAI-форматом ответа,
- 502 на timeout/ok=False,
- 400 на stream=true и пустой prompt.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openai_endpoint as oe
from agent_runner import AgentResult


# Маленькое валидное PNG (1x1, прозрачный) — base64.
_PNG_1x1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgAAIAAAUAAarVyFEAAAAASUVORK5CYII="
)


class _FakeRunner:
    """Имитация AgentRunner: ловит prompt и возвращает заранее подготовленный
    AgentResult, чтобы тест мог проверить и сборку prompt, и формат ответа."""

    def __init__(self, *, result: AgentResult | None = None, exc: Exception | None = None):
        self._result = result or AgentResult(
            ok=True, text="hello from cursor", raw_stdout="", raw_stderr="",
            exit_code=0, duration_sec=0.1, timed_out=False,
        )
        self._exc = exc
        self.captured_prompts: list[str] = []

    async def run(self, prompt: str) -> AgentResult:
        self.captured_prompts.append(prompt)
        if self._exc is not None:
            raise self._exc
        return self._result


def _build_app(runner: _FakeRunner, *, capture_models: list[str | None] | None = None) -> FastAPI:
    """Фабрика принимает `model=...` (новая сигнатура). Если caller передал
    `capture_models`, slug, который пришёл в фабрику, кладётся туда — это
    позволяет тестам убедиться, что валидированный slug действительно дотёк."""

    def _factory(*, model: str | None = None):
        if capture_models is not None:
            capture_models.append(model)
        return runner

    app = FastAPI()
    app.include_router(oe.make_router(runner_factory=_factory))
    return app


@pytest.fixture
def runner() -> _FakeRunner:
    return _FakeRunner()


@pytest.fixture
def client_with_key(
    runner: _FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> TestClient:
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    return TestClient(_build_app(runner))


def _payload_text(text: str = "What is 2+2?", *, model: str = "composer-2") -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": text},
        ],
    }


def test_disabled_when_no_api_key(monkeypatch: pytest.MonkeyPatch, runner: _FakeRunner, tmp_path: Path) -> None:
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    client = TestClient(_build_app(runner))
    r = client.post("/v1/chat/completions", json=_payload_text())
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"].lower()


def test_missing_bearer_returns_401(client_with_key: TestClient) -> None:
    r = client_with_key.post("/v1/chat/completions", json=_payload_text())
    assert r.status_code == 401


def test_wrong_bearer_returns_401(client_with_key: TestClient) -> None:
    r = client_with_key.post(
        "/v1/chat/completions",
        json=_payload_text(),
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_happy_path_text_only(client_with_key: TestClient, runner: _FakeRunner) -> None:
    r = client_with_key.post(
        "/v1/chat/completions",
        json=_payload_text("What is 2+2?"),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "composer-2"
    assert data["choices"][0]["message"]["content"] == "hello from cursor"
    assert data["choices"][0]["finish_reason"] == "stop"

    # prompt должен содержать оба сообщения с маркерами ролей.
    assert len(runner.captured_prompts) == 1
    prompt = runner.captured_prompts[0]
    assert "[system]" in prompt and "You are helpful." in prompt
    assert "[user]" in prompt and "What is 2+2?" in prompt


def test_image_url_decoded_to_tempfile(client_with_key: TestClient, runner: _FakeRunner, tmp_path: Path) -> None:
    b64 = base64.b64encode(_PNG_1x1).decode()
    payload = {
        "model": "composer-2",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look here"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
    }
    r = client_with_key.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 200, r.text

    prompt = runner.captured_prompts[0]
    assert "look here" in prompt
    assert "Analyze the image at this absolute path" in prompt
    # Файл должен быть записан в sandbox workspace, а после ответа — удалён.
    sandbox_images = tmp_path / "ws" / ".openai-images"
    leftover = list(sandbox_images.glob("cursor-img-*.png")) if sandbox_images.exists() else []
    assert leftover == [], f"tmp images were not cleaned: {leftover}"


def test_stream_true_returns_400(client_with_key: TestClient) -> None:
    payload = _payload_text() | {"stream": True}
    r = client_with_key.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 400
    assert "stream" in r.json()["detail"].lower()


def test_timed_out_returns_502(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = _FakeRunner(
        result=AgentResult(
            ok=False, text="", raw_stdout="", raw_stderr="",
            exit_code=None, duration_sec=600.0, timed_out=True,
        )
    )
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    client = TestClient(_build_app(runner))
    r = client.post(
        "/v1/chat/completions",
        json=_payload_text(),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 502
    assert "timed out" in r.json()["detail"].lower()


def test_agent_failure_returns_502(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = _FakeRunner(
        result=AgentResult(
            ok=False, text="", raw_stdout="", raw_stderr="boom",
            exit_code=1, duration_sec=0.1, timed_out=False, is_error=True,
        )
    )
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    client = TestClient(_build_app(runner))
    r = client.post(
        "/v1/chat/completions",
        json=_payload_text(),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 502


def test_empty_messages_returns_422(client_with_key: TestClient) -> None:
    r = client_with_key.post(
        "/v1/chat/completions",
        json={"model": "composer-2", "messages": []},
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 422


# === model routing / allowlist =====================================


def test_model_resolved_and_passed_to_factory(
    monkeypatch: pytest.MonkeyPatch,
    runner: _FakeRunner,
    tmp_path: Path,
) -> None:
    """`body.model` из allowlist дотекает до runner_factory как `model=...`."""
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    captured: list[str | None] = []
    client = TestClient(_build_app(runner, capture_models=captured))

    r = client.post(
        "/v1/chat/completions",
        json=_payload_text(model="composer-2"),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 200, r.text
    assert captured == ["composer-2"]


def test_model_default_passes_none(
    monkeypatch: pytest.MonkeyPatch,
    runner: _FakeRunner,
    tmp_path: Path,
) -> None:
    """Пустая строка / `default` = «модель из основного конфига», фабрика
    получает `model=None` и сама подставляет дефолт."""
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    captured: list[str | None] = []
    client = TestClient(_build_app(runner, capture_models=captured))

    r = client.post(
        "/v1/chat/completions",
        json=_payload_text(model="default"),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 200, r.text
    assert captured == [None]


def test_model_not_in_allowlist_returns_400(
    monkeypatch: pytest.MonkeyPatch,
    runner: _FakeRunner,
    tmp_path: Path,
) -> None:
    """Произвольный slug (например, легаси `cursor-agent`) даёт 400 — иначе
    клиент мог бы инжектить любую строку в `cursor-agent --model`."""
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    client = TestClient(_build_app(runner))

    r = client.post(
        "/v1/chat/completions",
        json=_payload_text(model="cursor-agent"),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 400
    assert "BRIDGE_OPENAI_ALLOWED_MODELS" in r.json()["detail"]


def test_model_allowlist_can_be_narrowed_via_env(
    monkeypatch: pytest.MonkeyPatch,
    runner: _FakeRunner,
    tmp_path: Path,
) -> None:
    """`BRIDGE_OPENAI_ALLOWED_MODELS=composer-2` режет дефолт до одной модели."""
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BRIDGE_OPENAI_ALLOWED_MODELS", "composer-2")
    client = TestClient(_build_app(runner))

    ok = client.post(
        "/v1/chat/completions",
        json=_payload_text(model="composer-2"),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert ok.status_code == 200, ok.text

    bad = client.post(
        "/v1/chat/completions",
        json=_payload_text(model="gpt-5"),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert bad.status_code == 400


def test_legacy_factory_without_model_kw_still_works(
    monkeypatch: pytest.MonkeyPatch,
    runner: _FakeRunner,
    tmp_path: Path,
) -> None:
    """Для обратной совместимости: фабрика без аргумента `model` тоже работает —
    эндпоинт ловит TypeError и зовёт её повторно без аргументов."""
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    legacy_calls = 0

    def _legacy_factory():
        nonlocal legacy_calls
        legacy_calls += 1
        return runner

    app = FastAPI()
    app.include_router(oe.make_router(runner_factory=_legacy_factory))
    client = TestClient(app)

    r = client.post(
        "/v1/chat/completions",
        json=_payload_text(model="composer-2"),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 200, r.text
    assert legacy_calls == 1


def test_max_prompt_chars_413(monkeypatch: pytest.MonkeyPatch, runner: _FakeRunner, tmp_path: Path) -> None:
    monkeypatch.setenv("BRIDGE_OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("CURSOR_OPENAI_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("BRIDGE_OPENAI_MAX_PROMPT_CHARS", "1500")
    client = TestClient(_build_app(runner))
    big_text = "A" * 5_000
    r = client.post(
        "/v1/chat/completions",
        json=_payload_text(big_text),
        headers={"Authorization": "Bearer secret-key"},
    )
    assert r.status_code == 413
