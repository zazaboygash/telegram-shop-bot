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

# CANONICAL ROUTE CONTRACT — must mirror the actual Next.js app tree (single source of truth
# shared by bot buttons and notifications; do not invent slugs that don't exist in the frontend).
ROUTES: dict[str, str] = {
    "home": "/",            # canonical Home is "/", NOT /home
    "store": "/store",
    "vpn": "/vpn",
    "orders": "/orders",
    "authenticator": "/authenticator",
    "profile": "/profile",
}


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


def _webapp_button(label: str, route_key: str, webapp_base: str) -> dict[str, str]:
    """InlineKeyboardButton.web_app with DIRECT HTTPS URL (no Main Mini App dependency).
    Route keys must exist in ROUTES contract (drift-proof against frontend changes)."""
    if route_key not in ROUTES:
        raise ValueError(f"unknown route key: {route_key}")
    if not webapp_base.startswith("https://"):
        raise ValueError("webapp base must be https")
    return {"text": label, "web_app": {"url": f"{webapp_base.rstrip('/')}{ROUTES[route_key]}"}}


def _url_button(label: str, url: str) -> dict[str, str]:
    if not url.startswith("https://"):
        raise ValueError("only https urls allowed")
    return {"text": label, "url": url}


@dataclass(slots=True)
class BotConfig:
    webapp_url: str  # https base of the shared Next.js Mini App
    bot_username: str = ""  # only needed for t.me deep links (external/shareable)
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
        b = self._cfg.webapp_url
        return [
            [_webapp_button("Open Gash", "home", b)],
            [_webapp_button("Store", "store", b), _webapp_button("VPN", "vpn", b)],
            [_webapp_button("Orders", "orders", b), _url_button("Support", self._cfg.support_url)],
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
        buttons.append([_webapp_button("Open Store", "store", self._cfg.webapp_url)])
        await self._t.send_message(user.chat_id, "\n".join(lines), buttons)

    # ── account and shop actions (all state remains in Core) ────────────
    async def authenticate(self, init_data: str, *, chat_id: int = 0) -> BotUser:
        """Exchange initData with Core; never parse or trust Telegram identity locally."""
        session = await self._core.telegram_session(init_data)
        return BotUser(chat_id=chat_id, gash_token=session["token"], display_name=session.get("display_name", "Gash user"))

    async def handle_balance(self, user: BotUser) -> None:
        wallet = await self._core.get_wallet(user.gash_token)
        if not wallet:
            await self._t.send_message(user.chat_id, "No wallet is available yet.", [[_webapp_button("Open profile", "profile", self._cfg.webapp_url)]])
            return
        amount = wallet.get("balance_minor", 0) / 100
        await self._t.send_message(user.chat_id, f"Balance: {amount:.2f} {wallet.get('currency', 'RUB')}", [[_webapp_button("Top up", "profile", self._cfg.webapp_url)]])

    async def handle_topup(self, user: BotUser, amount_minor: int, idempotency_key: str) -> None:
        payment = await self._core.create_topup(user.gash_token, amount_minor, idempotency_key=idempotency_key)
        pay_url = payment.get("pay_url")
        buttons = [[_url_button("Pay", pay_url)]] if pay_url else [[_webapp_button("Open profile", "profile", self._cfg.webapp_url)]]
        await self._t.send_message(user.chat_id, f"Top-up created: {amount_minor / 100:.2f} {payment.get('currency', 'RUB')}", buttons)

    async def handle_checkout(self, user: BotUser, wallet_id: str, variant_id: str, qty: int, idempotency_key: str) -> str:
        result = await self._core.create_order_idempotent(user.gash_token, wallet_id=wallet_id, variant_id=variant_id, qty=qty, idempotency_key=idempotency_key)
        order_id = str(result["order_id"])
        await self._t.send_message(user.chat_id, f"Order created: {order_id[:8]}…", [[_webapp_button("View order", "orders", self._cfg.webapp_url)]])
        return order_id

    # ── /orders (quick status, deep link to app) ────────────────────────
    async def handle_orders(self, user: BotUser, last_order_id: str | None) -> None:
        if not last_order_id:
            await self._t.send_message(
                user.chat_id,
                "No orders yet.",
                [[_webapp_button("Open Store", "store", self._cfg.webapp_url)]],
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
            user.chat_id, text, [[_webapp_button("Open Orders", "orders", self._cfg.webapp_url)]]
        )
