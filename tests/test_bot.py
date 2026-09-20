"""Tests for the thin Gash bot: menu, catalog listing, deep-link validation, order ownership."""

from __future__ import annotations

import asyncio

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
                "variants": [{"sku": "gtp-1", "title": "Standard license", "price_minor": 39900}],
            }
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
    assert by_text["Open Gash"]["web_app"]["url"] == "https://dev-app.example/home"
    assert by_text["Store"]["web_app"]["url"] == "https://dev-app.example/store"
    assert by_text["Support"]["url"].startswith("https://")


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
