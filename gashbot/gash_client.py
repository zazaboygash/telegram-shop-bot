"""GashCoreClient — the ONLY network boundary between the Telegram bot and Gash Core.

Gash Core owns catalog, identity, wallet, payment and order state. This module is
intentionally API-only: it never opens a local commerce database or ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class GashCoreError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class GashCoreConfig:
    base_url: str
    service_token: str = ""
    timeout_s: float = 10.0


class GashCoreClient:
    """Thin client. User-scoped operations require a Core-issued session token."""

    def __init__(self, config: GashCoreConfig) -> None:
        self._cfg = config

    def _headers(self, token: str | None) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _request(
        self, method: str, path: str, *, token: str | None = None,
        json: dict[str, Any] | None = None, service: bool = False,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = self._headers(token)
        if service:
            if not self._cfg.service_token:
                raise ValueError("service token is required for Telegram identity exchange")
            headers["X-Gash-Service"] = self._cfg.service_token
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(timeout=self._cfg.timeout_s) as client:
            resp = await client.request(method, f"{self._cfg.base_url}{path}", headers=headers, json=json)
        if resp.status_code >= 400:
            try:
                err = resp.json()["error"]
                raise GashCoreError(resp.status_code, err.get("code", "?"), err.get("message", ""))
            except (KeyError, ValueError):
                raise GashCoreError(resp.status_code, "UNKNOWN", resp.text[:200]) from None
        if resp.status_code == 204:
            return None
        return resp.json()

    async def telegram_session(self, init_data: str) -> dict[str, Any]:
        """Exchange raw Telegram initData for a server-verified Core session."""
        return await self._request("POST", "/api/v1/auth/telegram", json={"init_data": init_data}, service=True)

    async def list_products(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/products")
        return data.get("items", [])

    async def get_product(self, slug: str) -> dict[str, Any] | None:
        return next((item for item in await self.list_products() if item.get("slug") == slug), None)

    async def get_wallet(self, token: str) -> dict[str, Any] | None:
        data = await self._request("GET", "/api/v1/wallet/mine", token=token)
        return (data.get("items") or [None])[0]

    async def create_topup(self, token: str, amount_minor: int, *, currency: str = "RUB", idempotency_key: str) -> dict[str, Any]:
        if amount_minor <= 0:
            raise ValueError("top-up amount must be positive")
        return await self._request("POST", "/api/v1/payments/topup", token=token,
                                   json={"amount_minor": amount_minor, "currency": currency},
                                   idempotency_key=idempotency_key)

    async def create_order_idempotent(self, token: str, *, wallet_id: str, variant_id: str, qty: int, idempotency_key: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/orders/checkout", token=token,
                                   json={"wallet_id": wallet_id, "variant_id": variant_id, "qty": qty},
                                   idempotency_key=idempotency_key)

    async def list_orders(self, token: str) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/orders", token=token)
        return data.get("items", [])

    async def get_order(self, token: str, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/orders/{order_id}", token=token)
