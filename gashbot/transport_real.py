"""Real Bot API transport (httpx) for production/dev with token."""

from __future__ import annotations

import httpx

from gashbot.bot import TelegramTransport

API = "https://api.telegram.org"


class RealTelegramTransport(TelegramTransport):
    def __init__(self, bot_token: str) -> None:
        self._token = bot_token

    async def send_message(
        self, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{API}/bot{self._token}/sendMessage", json=payload)
            if not r.json().get("ok"):
                raise RuntimeError(f"sendMessage failed: {r.text[:200]}")

    async def answer_callback(self, callback_id: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{API}/bot{self._token}/answerCallbackQuery", json={"callback_query_id": callback_id})
