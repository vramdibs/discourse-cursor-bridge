"""Build the prompt fed to cursor-agent from a Discourse topic snapshot."""

from __future__ import annotations

from discourse_client import TopicSnapshot


SYSTEM_PREAMBLE = """\
Ты ассистент, отвечающий новым постом в Discourse-топике.
Workspace: ~/workspace. Сверяйся с правилами в .cursor/rules/.

Жёсткие требования к ответу:
- Один пост в формате markdown, без префиксов, без блоков типа "Plan:" или "Tool result:".
- Не обращайся к собеседнику в третьем лице, отвечай тому, кто тебя позвал.
- Не цитируй чужие посты целиком, ссылайся на номера ([#post_number]).
- Если запрос вне твоей зоны ответственности по правилам - честно скажи и предложи следующий шаг.
- Не публикуй секретов, точных production-эндпоинтов, имен коллег (см. discourse-publish.mdc).
"""


def build_prompt(
    snapshot: TopicSnapshot,
    triggering_post_id: int,
    triggering_username: str,
    bot_username: str,
    max_post_bytes: int,
) -> str:
    lines: list[str] = [SYSTEM_PREAMBLE.rstrip(), ""]
    lines.append(
        f'Тебя позвали в топике "{snapshot.title}" (id={snapshot.id}). '
        f"Тебя зовёт @{triggering_username}. Ты пишешь от имени @{bot_username}."
    )
    lines.append("")
    lines.append("Контекст последних постов (в хронологическом порядке):")
    lines.append("")
    for post in snapshot.posts:
        body = post.raw or ""
        if len(body.encode("utf-8")) > max_post_bytes:
            body = body.encode("utf-8")[:max_post_bytes].decode("utf-8", errors="ignore") + "\n[...truncated...]"
        marker = " <-- ТЕКУЩИЙ ЗАПРОС К ТЕБЕ" if post.id == triggering_post_id else ""
        lines.append(f"[#{post.post_number} @{post.username}]{marker}:")
        lines.append(body.rstrip())
        lines.append("")
    lines.append(
        "Сформулируй один итоговый пост-ответ. Верни только текст поста, без обрамляющих кавычек."
    )
    return "\n".join(lines)
