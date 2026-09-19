"""GashCoreClient — the ONLY network boundary between the Telegram bot and Gash Core.

Rules (docs/integrations/GASH_TELEGRAM_SHOP.md):
- no SQLAlchemy imports, no direct PostgreSQL access;
- all calls carry a Gash bearer token resolved from a VERIFIED Telegram identity
  (initData validated by Core, never trusted from raw callback data);
- service-to-server auth uses TELEGRAM_SERVICE_TOKEN (shared secret via env)
  sent as X-Gash-Service header for the identity-resolution call only.
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
    """Thin typed client. Every user-scoped call requires a Gash session token."""

    def __init__(self, config: GashCoreConfig) -> None:
        self._cfg = config

    # ── low level ────────────────────────────────────────────────────────
    def _headers(self, token: str | None) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict[str, Any] | None = None,
        service: bool = False,
    ) -> Any:
        headers = self._headers(token)
        if service:
            headers["X-Gash-Service"] = self._cfg.service_token
        async with httpx.AsyncClient(timeout=self._cfg.timeout_s) as client:
            resp = await client.request(
                method, f"{self._cfg.base_url}{path}", headers=headers, json=json
            )
        if resp.status_code >= 400:
            try:
                err = resp.json()["error"]
                raise GashCoreError(resp.status_code, err.get("code", "?"), err.get("message", ""))
            except (KeyError, ValueError):
                raise GashCoreError(resp.status_code, "UNKNOWN", resp.text[:200]) from None
        if resp.status_code == 204:
            return None
        return resp.json()

    # ── auth / identity ──────────────────────────────────────────────────
    async def telegram_session(self, init_data: str) -> dict[str, Any]:
        """initData (validated server-side) → Gash session {token, user_id, ...}."""
        return await self._request(
            "POST", "/api/v1/auth/telegram", json={"init_data": init_data}, service=True
        )

    # ── catalog ──────────────────────────────────────────────────────────
    async def list_products(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/products")
        return data.get("items", [])

    async def get_product(self, slug: str) -> dict[str, Any] | None:
        for item in await self.list_products():
            if item.get("slug") == slug:
                return item
        return None

    # ── wallet / orders ─────────────────────────────────────────────────
    async def get_wallet(self, token: str) -> dict[str, Any] | None:
        data = await self._request("GET", "/api/v1/wallet/mine", token=token)
        return (data.get("items") or [None])[0]

    async def create_order_idempotent(
        self, token: str, *, wallet_id: str, variant_id: str, qty: int, idempotency_key: str
    ) -> dict[str, Any]:
        """POST /orders/checkout with Idempotency-Key header (replay-safe)."""
        headers = self._headers(token)
        headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(timeout=self._cfg.timeout_s) as client:
            resp = await client.post(
                f"{self._cfg.base_url}/api/v1/orders/checkout",
                headers=headers,
                json={"wallet_id": wallet_id, "variant_id": variant_id, "qty": qty},
            )
        if resp.status_code >= 400:
            try:
                err = resp.json()["error"]
                raise GashCoreError(resp.status_code, err.get("code", "?"), err.get("message", ""))
            except (KeyError, ValueError):
                raise GashCoreError(resp.status_code, "UNKNOWN", resp.text[:200]) from None
        return resp.json()

    async def get_order(self, token: str, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/orders/{order_id}", token=token)

    # ── vpn ──────────────────────────────────────────────────────────────
    async def list_vpn_subscriptions(self, token: str) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/vpn/subscriptions", token=token)
        return data.get("items", [])
