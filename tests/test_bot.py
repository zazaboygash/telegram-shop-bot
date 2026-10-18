"""Tests for the thin Gash bot: menu, catalog listing, deep-link validation, order ownership."""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from gashbot.bot import BotConfig, BotUser, FakeTelegramTransport, GashBot
from gashbot.gash_client import GashCoreClient, GashCoreConfig


class FakeCore(GashCoreClient):
    """Overrides network calls with canned data (client surface stays real)."""

    def __init__(self) -> None:
        super().__init__(GashCoreConfig(base_url="http://fake"))
        self.orders: dict[str, dict] = {}

    async def list_products(self) -> list[dict]:
        return [
            {
                "slug": "gash-test-product",
                "title": "Gash Test Product",
                "kind": "LICENSE",
                "variants": [{"id": "gtp-1", "sku": "gtp-1", "title": "Standard license", "price_minor": 39900, "currency": "RUB"}],
            },
            {
                "slug": "vpn-promo-codes",
                "title": "VPN promo codes",
                "kind": "FILE",
                "description": "External VPN promo code delivery",
                # Deliberately sensitive fulfillment-like field: UX must ignore it.
                "code": "VPN-SECRET-DO-NOT-RENDER",
                "variants": [
                    {"id": "vpn-1m", "sku": "vpn-1m", "title": "VPN promo code — 1 month", "price_minor": 1000, "currency": "RUB"},
                    {"id": "vpn-3m", "sku": "vpn-3m", "title": "VPN promo code — 3 months", "price_minor": 2000, "currency": "RUB"},
                    {"id": "vpn-6m", "sku": "vpn-6m", "title": "VPN promo code — 6 months", "price_minor": 3000, "currency": "RUB"},
                    {"id": "vpn-12m", "sku": "vpn-12m", "title": "VPN promo code — 12 months", "price_minor": 4000, "currency": "RUB"},
                    {"id": "vpn-balance", "sku": "vpn-balance", "title": "VPN balance code", "price_minor": 5000, "currency": "RUB"},
                ],
            },
        ]

    async def get_order(self, token: str, order_id: str) -> dict:
        # ownership enforced by Core: foreign token → 404
        if token != "owner-token":
            from gashbot.gash_client import GashCoreError

            raise GashCoreError(404, "NOT_FOUND", "order not found")
        return self.orders[order_id]


def _bot() -> tuple[GashBot, FakeTelegramTransport]:
    t = FakeTelegramTransport()
    cfg = BotConfig(webapp_url="https://dev-app.example")
    return GashBot(t, FakeCore(), cfg), t


def test_start_menu_has_in_chat_funnel_and_separate_mini_app() -> None:
    bot, t = _bot()
    asyncio.run(bot.handle_start(BotUser(chat_id=1, gash_token="tok", display_name="D", language_code="ru")))
    flat = [button for row in t.sent[0].buttons for button in row]
    assert any(button.get("callback_data") == "store" for button in flat)
    assert any(button.get("callback_data") == "profile" for button in flat)
    assert any("web_app" in button for button in flat)
    assert any(button.get("callback_data") == "faq" for button in flat)


def test_start_menu_renders_startapp_deeplinks() -> None:
    bot, t = _bot()
    user = BotUser(chat_id=1, gash_token="tok", display_name="D")
    asyncio.run(bot.handle_start(user))
    assert "Gash's Lair" in t.sent[0].text
    flat = [b for row in t.sent[0].buttons for b in row]
    by_text = {b["text"]: b for b in flat}
    assert by_text["🟢 Open Mini App"]["web_app"]["url"] == "https://dev-app.example/"  # canonical Home
    assert by_text["🛍 Store"]["callback_data"] == "store"
    assert by_text["💬 Support"]["callback_data"] == "support"


def test_route_contract_matches_frontend() -> None:
    """Route drift guard: every bot route key maps to an EXISTING frontend page."""
    from gashbot.bot import ROUTES

    app_dir = pathlib.Path(__file__).resolve().parents[3] / "gash-ecosystem" / "apps" / "web" / "app"
    if not app_dir.exists():
        pytest.skip("frontend tree not available in this environment")
    for key, route in ROUTES.items():
        target = app_dir / route.strip("/")
        if route == "/":
            assert (app_dir / "page.tsx").exists(), "Home page missing"
        else:
            assert (target / "page.tsx").exists(), f"route {route} (key={key}) has no page.tsx"


def test_vpn_codes_group_core_catalog_and_never_render_delivery_code() -> None:
    bot, t = _bot()
    asyncio.run(bot.handle_vpn_codes(1))
    text = t.sent[-1].text
    for duration in ("1 month", "3 month", "6 month", "12 month"):
        assert duration in text
    assert "Balance codes" in text
    assert "VPN-SECRET" not in text
    assert "Mini App" in text
    assert "/vpn" in str(t.sent[-1].buttons)


def test_vpn_codes_show_core_price_and_not_unknown_placeholder_price() -> None:
    bot, t = _bot()
    asyncio.run(bot.handle_vpn_codes(1))
    text = t.sent[-1].text
    assert "10.00 RUB" in text
    assert "20.00 RUB" in text
    assert "50.00 RUB" in text
    assert "999" not in text


def test_store_does_not_expose_code_like_catalog_secret() -> None:
    bot, t = _bot()
    asyncio.run(bot.handle_store(BotUser(chat_id=1, gash_token="tok", display_name="D")))
    assert "VPN-SECRET" not in t.sent[-1].text
    assert "never accepts payment or delivers codes" in t.sent[-1].text


def test_profile_button_opens_shared_app() -> None:
    bot, t = _bot()
    asyncio.run(bot.handle_profile(1))
    assert "Профиль" in t.sent[0].text
    assert "/profile" in str(t.sent[0].buttons[0][0])


def test_store_lists_same_catalog() -> None:
    bot, t = _bot()
    user = BotUser(chat_id=1, gash_token="tok", display_name="D")
    asyncio.run(bot.handle_store(user))
    assert "Standard license" in t.sent[0].text
    assert "399" in t.sent[0].text


def test_orders_respects_ownership() -> None:
    bot, t = _bot()
    core = bot._core
    assert isinstance(core, FakeCore)
    core.orders["abc"] = {"id": "abcdef", "status": "PAID", "total_minor": 39900}

    owner = BotUser(chat_id=1, gash_token="owner-token", display_name="D")
    asyncio.run(bot.handle_orders(owner, "abc"))
    assert "PAID" in t.sent[0].text

    stranger = BotUser(chat_id=2, gash_token="foreign-token", display_name="X")
    asyncio.run(bot.handle_orders(stranger, "abc"))
    assert "unavailable" in t.sent[-1].text  # second send in this test


def test_route_and_url_validation() -> None:
    from gashbot.bot import _url_button, _webapp_button

    with pytest.raises(ValueError):
        _webapp_button("x", "../evil", "https://dev-app.example")
    with pytest.raises(ValueError):
        _webapp_button("x", "a" * 100, "https://dev-app.example")
    with pytest.raises(ValueError):
        _webapp_button("x", "home", "http://insecure.example")  # https only
    with pytest.raises(ValueError):
        _url_button("x", "http://insecure.example")


def test_store_renders_categories_and_variant_mini_app_buy_buttons() -> None:
    bot, t = _bot()
    asyncio.run(bot.handle_store(BotUser(chat_id=1, gash_token="tok", display_name="D")))

    sent = t.sent[-1]
    assert "Accounts" in sent.text
    assert "Digital goods" in sent.text
    assert "Standard license" in sent.text
    buy_buttons = [button for row in sent.buttons for button in row if button["text"].startswith("Buy")]
    assert len(buy_buttons) == 6
    assert all(button["web_app"]["url"].startswith("https://dev-app.example/store?") for button in buy_buttons)
    assert all("variant=" in button["web_app"]["url"] for button in buy_buttons)


def test_store_adds_safe_telegram_mini_app_deep_links_per_product() -> None:
    bot, t = GashBot(
        FakeTelegramTransport(),
        FakeCore(),
        BotConfig(webapp_url="https://dev-app.example", bot_username="gash_shop_bot"),
    ), FakeTelegramTransport()
    # Use the transport actually owned by the bot.
    bot._t = t
    asyncio.run(bot.handle_store(BotUser(chat_id=1, gash_token="tok", display_name="D")))
    deep_links = [
        button["url"]
        for row in t.sent[-1].buttons
        for button in row
        if button.get("text") == "Share"
    ]
    assert deep_links
    assert all(url.startswith("https://t.me/gash_shop_bot?") and "startapp=" in url for url in deep_links)
    assert all("VPN-SECRET" not in url for url in deep_links)


def test_store_has_no_direct_payment_action_or_payment_url() -> None:
    bot, t = _bot()
    assert not hasattr(bot, "handle_topup")
    assert not hasattr(bot, "handle_checkout")
    asyncio.run(bot.handle_store(BotUser(chat_id=1, gash_token="tok", display_name="D")))
    buttons = [button for row in t.sent[-1].buttons for button in row]
    assert all("pay" not in button["text"].lower() for button in buttons)
    assert all("payment" not in str(button).lower() for button in buttons)


def test_store_empty_and_core_error_states_are_actionable() -> None:
    class EmptyCore(FakeCore):
        async def list_products(self) -> list[dict]:
            return []

    class ErrorCore(FakeCore):
        async def list_products(self) -> list[dict]:
            from gashbot.gash_client import GashCoreError

            raise GashCoreError(503, "CORE_UNAVAILABLE", "catalog unavailable")

    for core, expected in ((EmptyCore(), "No products are available"), (ErrorCore(), "temporarily unavailable")):
        transport = FakeTelegramTransport()
        bot = GashBot(transport, core, BotConfig(webapp_url="https://dev-app.example"))
        asyncio.run(bot.handle_store(BotUser(chat_id=1, gash_token="tok", display_name="D")))
        assert expected in transport.sent[-1].text
        assert any("web_app" in button for row in transport.sent[-1].buttons for button in row)


def test_start_welcome_uses_telegram_language() -> None:
    bot, t = _bot()
    asyncio.run(bot.handle_start(BotUser(chat_id=1, gash_token="tok", display_name="D", language_code="ru")))
    assert "Добро пожаловать" in t.sent[-1].text
    asyncio.run(bot.handle_start(BotUser(chat_id=2, gash_token="tok", display_name="D", language_code="en")))
    assert "Welcome" in t.sent[-1].text
