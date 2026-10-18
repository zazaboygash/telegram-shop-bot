"""Gash Telegram bot — thin entry point over the shared Mini App.

The bot is an entry point + notification layer, NOT a second UI and NOT a
commerce source. All state lives in Gash Core (docs/integrations/GASH_TELEGRAM_SHOP.md).

Dev transport: FakeTelegramTransport (no network, no token) for tests/local runs.
Real transport: aiogram or raw Bot API calls — wired in a later PR when the dev
bot token is provisioned in the fork's .env (TELEGRAM_BOT_TOKEN, never committed).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from gashbot.gash_client import GashCoreClient, GashCoreError

_STARTAPP_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

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

    async def send_message(self, chat_id: int, text: str, buttons: list[list[dict[str, Any]]] | None = None) -> None: ...

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


def _webapp_button(label: str, route_key: str, webapp_base: str) -> dict[str, Any]:
    """InlineKeyboardButton.web_app with DIRECT HTTPS URL (no Main Mini App dependency).
    Route keys must exist in ROUTES contract (drift-proof against frontend changes)."""
    if route_key not in ROUTES:
        raise ValueError(f"unknown route key: {route_key}")
    parsed = urlsplit(webapp_base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("webapp base must be https")
    return {"text": label, "web_app": {"url": f"{webapp_base.rstrip('/')}{ROUTES[route_key]}"}}


def _url_button(label: str, url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("only https urls allowed")
    return {"text": label, "url": url}


def _variant_webapp_button(label: str, variant: dict[str, Any], webapp_base: str) -> dict[str, Any]:
    """Open the shared store route with a Core variant selector, never a payment URL."""
    key = str(variant.get("id") or variant.get("sku") or "")
    if not key or not _STARTAPP_SLUG_RE.fullmatch(key):
        raise ValueError("catalog variant needs a safe id or sku")
    button = _webapp_button(label, "store", webapp_base)
    button["web_app"]["url"] = f"{webapp_base.rstrip('/')}{ROUTES['store']}?{urlencode({'variant': key})}"
    return button


def _variant_deep_link(label: str, product: dict[str, Any], variant: dict[str, Any], config: "BotConfig") -> dict[str, str] | None:
    """Build an optional safe t.me Mini App link for sharing a catalog item."""
    if not config.bot_username or not _BOT_USERNAME_RE.fullmatch(config.bot_username):
        return None
    slug = str(product.get("slug") or "")
    sku = str(variant.get("sku") or "")
    if not _STARTAPP_SLUG_RE.fullmatch(slug) or not _STARTAPP_SLUG_RE.fullmatch(sku):
        return None
    return _url_button(label, f"https://t.me/{config.bot_username}?{urlencode({'startapp': f'store_{slug}_{sku}'})}")


_DURATION_RE = re.compile(r"(?<!\d)(1|3|6|12)\s*(?:month|months|месяц|месяца|месяцев|мес)", re.IGNORECASE)
_CODE_RE = re.compile(r"promo|voucher|code|код|промокод|ваучер", re.IGNORECASE)
_BALANCE_RE = re.compile(r"balance|баланс|top[-_ ]?up|пополн", re.IGNORECASE)


def _catalog_text(product: dict[str, Any], variant: dict[str, Any] | None = None) -> str:
    """Build matching text without reading stock, secrets, or fulfillment payloads."""
    values = [product.get("title"), product.get("slug"), product.get("description"), product.get("kind")]
    if variant:
        values.extend((variant.get("title"), variant.get("sku")))
    return " ".join(str(value) for value in values if value is not None)


def _duration_for(product: dict[str, Any], variant: dict[str, Any]) -> str | None:
    match = _DURATION_RE.search(_catalog_text(product, variant))
    return match.group(1) if match else None


def _is_balance_product(product: dict[str, Any], variant: dict[str, Any]) -> bool:
    return bool(_BALANCE_RE.search(_catalog_text(product, variant)))


def _is_code_product(product: dict[str, Any], variant: dict[str, Any]) -> bool:
    text = _catalog_text(product, variant)
    return bool(_CODE_RE.search(text)) or (_duration_for(product, variant) is not None and "vpn" in text.lower())


def _variant_label(product: dict[str, Any], variant: dict[str, Any]) -> str:
    """Render only catalog-owned display fields; never print a delivered code."""
    title = html.escape(str(variant.get("title") or product.get("title") or "VPN code"))
    price_minor = variant.get("price_minor")
    currency = str(variant.get("currency") or "").strip()
    if isinstance(price_minor, int) and currency:
        return f"• {title} — {price_minor / 100:.2f} {html.escape(currency)}"
    return f"• {title} — price shown in Gash Mini App"


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
    language_code: str = "en"


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
            [_webapp_button("Store", "store", b), _webapp_button("VPN codes", "vpn", b)],
            [_webapp_button("Orders", "orders", b), _webapp_button("Profile", "profile", b)],
            [_url_button("Support", self._cfg.support_url)],
        ]

    # ── /start ───────────────────────────────────────────────────────────
    async def handle_start(self, user: BotUser) -> None:
        if user.language_code.lower().startswith("ru"):
            text = "Добро пожаловать в Gash's Lair\nVPN · Магазин · 2FA · Сервисы"
        else:
            text = "Welcome to Gash's Lair\nVPN · Store · 2FA · Services"
        await self._t.send_message(user.chat_id, text, self._menu_buttons())

    # ── /store: Core catalog, with every buy action in shared Mini App ──
    async def handle_store(self, user: BotUser) -> None:
        try:
            products = await self._core.list_products()
        except Exception:  # noqa: BLE001 — upstream/network failures get an actionable state
            await self._send_store_error(user.chat_id)
            return

        active_products = [
            (product, [v for v in product.get("variants", []) if v.get("active", True)])
            for product in products
        ]
        active_products = [(product, variants) for product, variants in active_products if variants]
        if not active_products:
            await self._t.send_message(
                user.chat_id,
                "The shelves are quiet.\nNo products are available right now. Check back soon.",
                [[_webapp_button("Open Store", "store", self._cfg.webapp_url)], [_url_button("Support", self._cfg.support_url)]],
            )
            return

        category_names = {"LICENSE": "Accounts", "VPN_PLAN": "VPN", "FILE": "Digital goods", "SERVICE": "Services"}
        categories: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for product, variants in active_products:
            category = category_names.get(str(product.get("kind", "")).upper(), "Other")
            categories.setdefault(category, []).extend((product, variant) for variant in variants)

        lines = ["<b>Gash Store</b>", "Live catalog from Gash Core — choose a product to open checkout in the Mini App.", ""]
        buttons: list[list[dict[str, Any]]] = []
        for category, entries in categories.items():
            lines.append(f"<b>{html.escape(category)}</b>")
            for product, variant in entries:
                title = html.escape(str(variant.get("title") or product.get("title") or "Product"))
                lines.append(f"<b>{html.escape(str(product.get('title') or product.get('slug') or 'Product'))}</b> — {_variant_label(product, variant)}")
                row: list[dict[str, Any]] = [_variant_webapp_button(f"Buy {title}", variant, self._cfg.webapp_url)]
                deep_link = _variant_deep_link("Share", product, variant, self._cfg)
                if deep_link:
                    row.append(deep_link)
                buttons.append(row)
            lines.append("")
        lines.extend(("Prices and availability are always read from Core.", "Payment is completed only in the secure Gash Mini App; the bot never accepts payment or delivers codes."))
        buttons.extend(([_webapp_button("Open full Store", "store", self._cfg.webapp_url)], [_url_button("Support", self._cfg.support_url)]))
        await self._t.send_message(user.chat_id, "\n".join(lines), buttons)

    async def _send_store_error(self, chat_id: int) -> None:
        await self._t.send_message(
            chat_id,
            "The catalog is temporarily unavailable. Please try again in a moment.",
            [[_webapp_button("Open Store", "store", self._cfg.webapp_url)], [_url_button("Support", self._cfg.support_url)]],
        )

    async def handle_vpn_codes(self, chat_id: int) -> None:
        """Present Core-configured VPN code variants, without inventing prices or inventory."""
        products = await self._core.list_products()
        grouped: dict[str, list[str]] = {duration: [] for duration in ("1", "3", "6", "12")}
        balance: list[str] = []
        other: list[str] = []
        for product in products:
            for variant in product.get("variants", []):
                if not variant.get("active", True) or not (_is_code_product(product, variant) or _is_balance_product(product, variant)):
                    continue
                label = _variant_label(product, variant)
                duration = _duration_for(product, variant)
                if _is_balance_product(product, variant):
                    balance.append(label)
                elif duration in grouped:
                    grouped[duration].append(label)
                else:
                    other.append(label)

        lines = ["<b>VPN promo codes</b>", "Choose a Core catalog product below. Prices are live from Core; no code is shown before delivery.", ""]
        for duration in ("1", "3", "6", "12"):
            lines.append(f"<b>{duration} month</b>")
            lines.extend(grouped[duration] or ["• Not currently listed in Core"])
        lines.append("<b>Balance codes</b>")
        lines.extend(balance or ["• Not currently listed in Core"])
        if other:
            lines.extend(("<b>Other Core-configured code products</b>", *other))
        lines.extend(("", "Delivery: place an order in Gash Mini App, then open the order details for instructions, bot link, and code after fulfillment."))
        await self._t.send_message(
            chat_id,
            "\n".join(lines),
            [[_webapp_button("Buy in Mini App", "vpn", self._cfg.webapp_url)], [_webapp_button("My orders", "orders", self._cfg.webapp_url)]],
        )

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

    async def handle_profile(self, chat_id: int) -> None:
        """Open the shared profile; balances stay in Core and require WebApp auth."""
        await self._t.send_message(
            chat_id,
            "Профиль, баланс и заказы открываются в защищённом Gash Mini App.",
            [[_webapp_button("Профиль и баланс", "profile", self._cfg.webapp_url)],
             [_webapp_button("Пополнить баланс", "profile", self._cfg.webapp_url)]],
        )

    async def handle_accounts(self, chat_id: int) -> None:
        """Show account products from the Core catalog; credentials never pass through the bot."""
        products = [p for p in await self._core.list_products() if p.get("kind") == "LICENSE"]
        lines = ["Аккаунты Gash — актуальные товары из Core:\n"]
        for product in products[:8]:
            variants = product.get("variants", [])
            if variants:
                variant = variants[0]
                lines.append(f"• {product.get('title', 'Account')} — {variant.get('title', '')}")
        if not products:
            lines.append("Сейчас доступных аккаунтов нет.")
        await self._t.send_message(
            chat_id,
            "\n".join(lines),
            [[_webapp_button("Открыть магазин", "store", self._cfg.webapp_url)]],
        )

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
