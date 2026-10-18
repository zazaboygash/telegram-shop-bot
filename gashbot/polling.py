"""Dev polling loop for the Gash bot (long polling via raw Bot API + httpx).

Dev-only runner: no webhook, no extra framework. Token comes from env
TELEGRAM_BOT_TOKEN (never logged). Handles /start, /store, /orders with the
same GashBot logic tested in tests/test_bot.py.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from gashbot.bot import BotConfig, BotUser, GashBot, _webapp_button
from gashbot.gash_client import GashCoreClient, GashCoreConfig
from gashbot.transport_real import RealTelegramTransport

API = "https://api.telegram.org"
BOT_COMMANDS = [
    {"command": "start", "description": "Open Gash menu"},
    {"command": "vpn_codes", "description": "VPN promo and balance codes"},
    {"command": "store", "description": "Browse Core catalog"},
    {"command": "accounts", "description": "Browse account products"},
    {"command": "profile", "description": "Open profile and balance"},
    {"command": "orders", "description": "Open my orders"},
    {"command": "help", "description": "Show help and delivery"},
]


async def _call(bot_token: str, method: str, **payload: Any) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(f"{API}/bot{bot_token}/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Bot API {method}: {data}")
        return data["result"]


async def main() -> None:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    core_url = os.environ.get("GASH_CORE_URL", "http://127.0.0.1:8100")
    webapp_url = os.environ.get("PUBLIC_WEB_URL", "https://dev-app.gashvpn.space")
    support_url = os.environ.get("SUPPORT_URL", "https://t.me/gashsupport")

    me = await _call(bot_token, "getMe")
    username: str = me["username"]
    await _call(bot_token, "setMyCommands", commands=BOT_COMMANDS)

    transport = RealTelegramTransport(bot_token)
    core = GashCoreClient(GashCoreConfig(base_url=core_url))
    bot = GashBot(transport, core, BotConfig(webapp_url=webapp_url, bot_username=username, support_url=support_url))

    offset = 0
    print(f"gashbot polling as @{username}…", flush=True)
    while True:
        try:
            updates = await _call(bot_token, "getUpdates", timeout=25, offset=offset)
        except Exception as exc:  # noqa: BLE001 — poll loop must survive
            print("poll error:", exc, flush=True)
            await asyncio.sleep(3)
            continue
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            callback = update.get("callback_query")
            if callback:
                chat_id = callback["message"]["chat"]["id"]
                callback_data = callback.get("data", "")
                await transport.answer_callback(callback["id"])
                if callback_data == "store":
                    await bot.handle_store_menu(chat_id)
                elif callback_data.startswith("cat:"):
                    await bot.handle_category(chat_id, callback_data.split(":", 1)[1])
                elif callback_data == "home":
                    await bot.handle_start(BotUser(chat_id=chat_id, gash_token="", display_name=""))
                elif callback_data == "profile":
                    await bot.handle_profile(chat_id)
                elif callback_data == "support":
                    await bot.handle_support(chat_id)
                elif callback_data == "faq":
                    await bot.handle_faq(chat_id)
                elif callback_data == "rules":
                    await bot.handle_rules(chat_id)
                elif callback_data == "language":
                    await bot.handle_language(chat_id)
                elif callback_data in {"lang_ru", "lang_en"}:
                    lang = "Русский" if callback_data == "lang_ru" else "English"
                    await transport.send_message(chat_id, f"Язык выбран: {lang} / Language selected: {lang}", bot._menu_buttons("ru" if callback_data == "lang_ru" else "en"))
                continue
            msg = update.get("message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            text = (msg.get("text") or "").split("@")[0]  # strip @bot suffix
            # BotUser requires a gash session; for menu-only commands a placeholder is
            # fine because handlers never call user-scoped APIs with it unless needed.
            user = BotUser(
                chat_id=chat_id,
                gash_token="",
                display_name=msg["from"].get("first_name", ""),
                language_code=msg["from"].get("language_code", "en"),
            )
            try:
                if text == "/start":
                    await bot.handle_start(user)
                elif text == "/store":
                    await bot.handle_store(user)
                elif text in {"/vpn", "/vpn_codes", "/promocodes", "/promo", "/коды", "/промокоды"}:
                    await bot.handle_vpn_codes(chat_id)
                elif text in {"/profile", "/balance"}:
                    await bot.handle_profile(chat_id)
                elif text in {"/accounts", "/аккаунты", "/account"}:
                    await bot.handle_accounts(chat_id)
                elif text == "/orders":
                    await bot.handle_orders(user, None)
                elif text in {"/help", "/помощь"}:
                    await transport.send_message(
                        chat_id,
                        "Команды: /start · /store · /vpn_codes · /accounts · /profile · /balance · /orders.\nVPN codes are bought in the Gash Mini App and delivered in the Core order details after fulfillment.",
                        [[_webapp_button("Open Mini App", "vpn", webapp_url)]],
                    )
                else:
                    await transport.send_message(
                        chat_id,
                        "Команды: /start · /store · /vpn_codes · /accounts · /profile · /orders — или откройте Gash.",
                    )
            except Exception as exc:  # noqa: BLE001
                print("handler error:", exc, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
