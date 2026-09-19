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

from gashbot.bot import BotConfig, BotUser, GashBot
from gashbot.gash_client import GashCoreClient, GashCoreConfig
from gashbot.transport_real import RealTelegramTransport

API = "https://api.telegram.org"


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
            msg = update.get("message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            text = (msg.get("text") or "").split("@")[0]  # strip @bot suffix
            # BotUser requires a gash session; for menu-only commands a placeholder is
            # fine because handlers never call user-scoped APIs with it unless needed.
            user = BotUser(chat_id=chat_id, gash_token="", display_name=msg["from"].get("first_name", ""))
            try:
                if text == "/start":
                    await bot.handle_start(user)
                elif text == "/store":
                    await bot.handle_store(user)
                elif text == "/orders":
                    await bot.handle_orders(user, None)
                else:
                    await transport.send_message(
                        chat_id,
                        "Commands: /start · /store · /orders — or open Gash from the menu button.",
                    )
            except Exception as exc:  # noqa: BLE001
                print("handler error:", exc, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
