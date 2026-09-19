"""Gash Telegram bot — thin entry point over the shared Mini App.

The bot is an entry point + notification layer, NOT a second UI and NOT a
commerce source. All state lives in Gash Core (docs/integrations/GASH_TELEGRAM_SHOP.md).

Dev transport: FakeTelegramTransport (no network, no token) for tests/local runs.
Real transport: aiogram or raw Bot API calls — wired in a later PR when the dev
bot token is provisioned in the fork's .env (TELEGRAM_BOT_TOKEN, never committed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from gashbot.gash_client import GashCoreClient, GashCoreError

_STARTAPP_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


class TelegramTransport(Protocol):
    """Minimal surface the bot needs from a Telegram transport."""

    async def send_message(self, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None) -> None: ...

    async def answer_callback(self, callback_id: str) -> None: ...


@dataclass(slots=True)
class FakeSent:
    chat_id: int
    text: str
    buttons: list[list[dict[str, str]]]


class FakeTelegramTransport:
    """In-memory transport for tests/dev without a bot token."""

    def __init__(self) -> None:
        self.sent: list[FakeSent] = []
        self.answered: list[str] = []

    async def send_message(
        self, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> None:
        self.sent.append(FakeSent(chat_id, text, buttons or []))

    async def answer_callback(self, callback_id: str) -> None:
        self.answered.append(callback_id)


def _webapp_button(label: str, startapp: str, bot_username: str) -> dict[str, str]:
    """Deep link button: official syntax https://t.me/<bot>?startapp=<slug> opens the Mini App
    on the mapped route. Slug is validated (untrusted input rule)."""
    if not _STARTAPP_SLUG_RE.fullmatch(startapp):
        raise ValueError("invalid startapp slug")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", bot_username):
        raise ValueError("invalid bot username")
    return {"text": label, "url": f"https://t.me/{bot_username}?startapp={startapp}"}


def _url_button(label: str, url: str) -> dict[str, str]:
    if not url.startswith("https://"):
        raise ValueError("only https urls allowed")
    return {"text": label, "url": url}


@dataclass(slots=True)
class BotConfig:
    webapp_url: str  # https base of the shared Next.js Mini App
    bot_username: str  # without @; buttons deep-link to t.me/<username>?startapp=<slug>
    support_url: str = "https://t.me/"  # configured via env later


@dataclass(slots=True)
class BotUser:
    """Verified context carried through handling. Never accept raw IDs from callbacks."""

    chat_id: int
    gash_token: str
    display_name: str


class GashBot:
    def __init__(
        self,
        transport: TelegramTransport,
        core: GashCoreClient,
        config: BotConfig,
    ) -> None:
        self._t = transport
        self._core = core
        self._cfg = config

    def _menu_buttons(self) -> list[list[dict[str, str]]]:
        u = self._cfg.bot_username
        return [
            [_webapp_button("Open Gash", "home", u)],
            [_webapp_button("Store", "store", u), _webapp_button("VPN", "vpn", u)],
            [_webapp_button("Orders", "orders", u), _url_button("Support", self._cfg.support_url)],
        ]

    # ── /start ───────────────────────────────────────────────────────────
    async def handle_start(self, user: BotUser) -> None:
        text = (
            "Gash's Lair\n"
            "VPN · Store · 2FA · Services"
        )
        await self._t.send_message(user.chat_id, text, self._menu_buttons())

    # ── /store (thin catalog listing, Mini App is the real UI) ──────────
    async def handle_store(self, user: BotUser) -> None:
        products = await self._core.list_products()
        lines: list[str] = ["Store — same catalog as the app:\n"]
        buttons: list[list[dict[str, str]]] = []
        for item in products[:8]:
            for variant in item.get("variants", [])[:1]:
                price = variant.get("price_minor", 0) / 100
                lines.append(f"• {item['title']} — {variant['title']} — {price:.0f} ₽")
        lines.append("\nOpen the app to buy:")
        buttons.append([_webapp_button("Open Store", "store", self._cfg.bot_username)])
        await self._t.send_message(user.chat_id, "\n".join(lines), buttons)

    # ── /orders (quick status, deep link to app) ────────────────────────
    async def handle_orders(self, user: BotUser, last_order_id: str | None) -> None:
        if not last_order_id:
            await self._t.send_message(
                user.chat_id, "No orders yet.", [[_webapp_button("Open Store", "store", self._cfg.bot_username)]]
            )
            return
        try:
            order = await self._core.get_order(user.gash_token, last_order_id)
        except GashCoreError as exc:
            # ownership enforced by Core (404 for foreign IDs)
            await self._t.send_message(user.chat_id, f"Order unavailable ({exc.code}).")
            return
        text = f"Order {order['id'][:8]}… — {order['status']} — {order['total_minor'] / 100:.0f} ₽"
        await self._t.send_message(
            user.chat_id, text, [[_webapp_button("Open Orders", "orders", self._cfg.bot_username)]]
        )
