"""
Thin async HTTP client for the Kalshi REST API.

Handles:
  - Authentication (RSA key signing per Kalshi's auth spec)
  - Retry with exponential backoff
  - Rate limit awareness (429 handling)

All methods return raw parsed JSON dicts. Parsing into domain models
happens in parser.py — this layer only speaks HTTP.

Kalshi authentication: each request is signed with an RSA private key.
The Authorization header format is:
  Authorization: <api-key>:<base64(RSA-SHA256(timestamp + method + path))>
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from typing import Any

import aiohttp

from bot.logging.logger import get_logger

log = get_logger(__name__)


class KalshiHttpClient:
    """
    Async HTTP client for Kalshi's trading API.

    Usage:
        async with KalshiHttpClient(cfg) as client:
            market = await client.get_market("SOME-TICKER")
            book = await client.get_orderbook("SOME-TICKER")
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_seconds
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "KalshiHttpClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        """
        Build Kalshi authentication headers.

        Kalshi uses RSA-SHA256 signing. The signature covers:
            timestamp (ms) + method.upper() + path (without query string)

        Note: this implementation uses HMAC-SHA256 as a placeholder for
        the structure. Replace with RSA-SHA256 once you have your real
        API credentials — see Kalshi's auth docs for the exact algorithm.

        TODO: Replace _sign_request with RSA-SHA256 once you have credentials.
              The signing key is your PEM private key from the Kalshi dashboard.
        """
        import hmac

        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + path.split("?")[0]
        sig = hmac.new(
            self._api_secret.encode(),
            msg.encode(),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(sig).decode()
        return {
            "Authorization": f"{self._api_key}:{signature}",
            "Content-Type": "application/json",
            "KALSHI-TIMESTAMP": ts,
            "KALSHI-ACCESS-KEY": self._api_key,
        }

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a request with retry/backoff. Raises on non-2xx after retries."""
        assert self._session is not None, "KalshiHttpClient.start() must be called first"
        url = f"{self._base_url}{path}"
        headers = self._sign_request(method, path)

        for attempt in range(self._max_retries):
            try:
                async with self._session.request(
                    method, url, headers=headers, params=params, json=json
                ) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", self._retry_backoff))
                        log.warning(
                            "rate_limited",
                            path=path,
                            retry_after=retry_after,
                            attempt=attempt,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if resp.status >= 400:
                        body = await resp.text()
                        log.error("http_error", status=resp.status, path=path, body=body[:200])
                        resp.raise_for_status()

                    return await resp.json()  # type: ignore[no-any-return]

            except aiohttp.ClientError as exc:
                if attempt == self._max_retries - 1:
                    raise
                wait = self._retry_backoff * (2 ** attempt)
                log.warning("request_failed_retrying", path=path, attempt=attempt, wait=wait, error=str(exc))
                await asyncio.sleep(wait)

        raise RuntimeError(f"All {self._max_retries} retries exhausted for {method} {path}")

    # ------------------------------------------------------------------
    # Market endpoints
    # ------------------------------------------------------------------

    async def get_market(self, ticker: str) -> dict[str, Any]:
        """GET /markets/{ticker} — market metadata."""
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        """GET /markets/{ticker}/orderbook — current order book snapshot."""
        data = await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return data.get("orderbook", data)

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /markets — paginated list of markets."""
        params: dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params)

    # ------------------------------------------------------------------
    # Order endpoints (used by KalshiExecutionEngine in Step 5)
    # ------------------------------------------------------------------

    async def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /orders — submit a new order."""
        data = await self._request("POST", "/orders", json=payload)
        return data.get("order", data)

    async def cancel_order(self, exchange_order_id: str) -> dict[str, Any]:
        """DELETE /orders/{order_id} — cancel an open order."""
        return await self._request("DELETE", f"/orders/{exchange_order_id}")

    async def get_order(self, exchange_order_id: str) -> dict[str, Any]:
        """GET /orders/{order_id} — fetch order status."""
        data = await self._request("GET", f"/orders/{exchange_order_id}")
        return data.get("order", data)

    async def get_open_orders(self) -> list[dict[str, Any]]:
        """GET /orders — fetch all open orders."""
        data = await self._request("GET", "/orders", params={"status": "open"})
        return data.get("orders", [])  # type: ignore[return-value]

    async def get_positions(self) -> list[dict[str, Any]]:
        """GET /portfolio/positions — fetch current positions from exchange."""
        data = await self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])  # type: ignore[return-value]
