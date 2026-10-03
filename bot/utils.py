"""Утилиты форматирования и нарезки сообщений."""
from __future__ import annotations

import html
from collections.abc import Iterable

# Telegram жёстко режет сообщения в 4096 символов. Держим запас.
SAFE_CHUNK = 3500


def esc(text: str) -> str:
    """Экранирование пользовательского текста под HTML parse_mode."""
    return html.escape(text, quote=False)


def progress_bar(done: int, total: int, width: int = 18) -> str:
    """Полоса прогресса: ██████████░░░░░░░░."""
    if total <= 0:
        return "░" * width
    filled = min(width, int(width * done / total))
    return "█" * filled + "░" * (width - filled)


def chunk_lines(lines: Iterable[str], limit: int = SAFE_CHUNK) -> list[str]:
    """Склеивает строки в блоки ≤ limit символов, разрезая по строкам."""
    chunks: list[str] = []
    buffer: list[str] = []
    size = 0
    for line in lines:
        add = len(line) + 1
        if size + add > limit and buffer:
            chunks.append("\n".join(buffer))
            buffer, size = [], 0
        buffer.append(line)
        size += add
    if buffer:
        chunks.append("\n".join(buffer))
    return chunks