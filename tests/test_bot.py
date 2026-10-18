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
                "variants": [{"sku": "gtp-1", "title": "Standard license", "price_minor": 39900, "currency": "RUB"}],
            },
            {
                "slug": "vpn-promo-codes",
                "title": "VPN promo codes",
                "kind": "FILE",
                "description": "External VPN promo code delivery",
                # Deliberately sensitive fulfillment-like field: UX must ignore it.
                "code": "VPN-SECRET-DO-NOT-RENDER",
                "variants": [
                    {"sku": "vpn-1m", "title": "VPN promo code — 1 month", "price_minor": 1000, "currency": "RUB"},
                    {"sku": "vpn-3m", "title": "VPN promo code — 3 months", "price_minor": 2000, "currency": "RUB"},
                    {"sku": "vpn-6m", "title": "VPN promo code — 6 months", "price_minor": 3000, "currency": "RUB"},
                    {"sku": "vpn-12m", "title": "VPN promo code — 12 months", "price_minor": 4000, "currency": "RUB"},
                    {"sku": "vpn-balance", "title": "VPN balance code", "price_minor": 5000, "currency": "RUB"},
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


def test_start_menu_renders_startapp_deeplinks() -> None:
    bot, t = _bot()
    user = BotUser(chat_id=1, gash_token="tok", display_name="D")
    asyncio.run(bot.handle_start(user))
    assert "Gash's Lair" in t.sent[0].text
    flat = [b for row in t.sent[0].buttons for b in row]
    by_text = {b["text"]: b for b in flat}
    assert by_text["Open Gash"]["web_app"]["url"] == "https://dev-app.example/"  # canonical Home
    assert by_text["Store"]["web_app"]["url"] == "https://dev-app.example/store"
    assert by_text["Support"]["url"].startswith("https://")


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
    assert "The bot never displays a code" in t.sent[-1].text


def test_profile_button_opens_shared_app() -> None:
    bot, t = _bot()
    asyncio.run(bot.handle_profile(1))
    assert "Профиль" in t.sent[0].text
    assert "/profile" in str(t.sent[0].buttons[0][0])


def test_store_lists_same_catalog() -> None:
    bot, t = _bot()
    user = BotUser(chat_id=1, gash_token="tok", display_name="D")
    asyncio.run(bot.handle_store(user))
    assert "Gash Test Product" in t.sent[0].text
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
