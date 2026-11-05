"""Async HTTP client for the Ramp Developer API.

Handles OAuth client-credentials tokens (cached until shortly before expiry),
retries with backoff for rate limits / timeouts on safe requests, and
keyset pagination via ``page.next``.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
RETRY_STATUSES = {429, 502, 503, 504}


class RampError(Exception):
    def __init__(self, status: int, message: str, body: Any = None, trace_id: str | None = None):
        self.status = status
        self.body = body
        self.trace_id = trace_id
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": str(self), "status": self.status}
        if self.body is not None:
            out["details"] = self.body
        if self.trace_id:
            out["x_trace_id"] = self.trace_id
        return out


class RampClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "ramp-mcp/0.1"},
        )
        self._token: str | None = settings.access_token
        self._token_expiry: float = float("inf") if settings.access_token else 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth
    async def _get_token(self, force: bool = False) -> str:
        if self.settings.access_token and not (self.settings.client_id and self.settings.client_secret):
            return self.settings.access_token
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_expiry:
                return self._token
            if not (self.settings.client_id and self.settings.client_secret):
                raise RampError(
                    0,
                    "Ramp credentials are not configured. Set RAMP_CLIENT_ID and "
                    "RAMP_CLIENT_SECRET (or RAMP_ACCESS_TOKEN).",
                )
            data = {"grant_type": "client_credentials"}
            if self.settings.scopes:
                data["scope"] = self.settings.scopes
            resp = await self._http.post(
                "/developer/v1/token",
                data=data,
                auth=(self.settings.client_id, self.settings.client_secret),
            )
            if resp.status_code != 200:
                raise _error_from_response(resp, prefix="Token request failed")
            payload = resp.json()
            self._token = payload["access_token"]
            expires_in = float(payload.get("expires_in", 3600))
            # Refresh a little early to avoid using a token at the edge of expiry.
            self._token_expiry = time.time() + max(expires_in - 120, 30)
            return self._token

    # --------------------------------------------------------------- request
    async def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        method = method.upper()
        url = self._normalize(path_or_url)
        params = _clean_params(params)
        attempt = 0
        refreshed = False
        while True:
            token = await self._get_token()
            try:
                resp = await self._http.request(
                    method,
                    url,
                    params=params or None,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if method in SAFE_METHODS and attempt < self.settings.max_retries:
                    attempt += 1
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise RampError(0, f"Network error calling Ramp: {exc.__class__.__name__}: {exc}") from exc

            if resp.status_code == 401 and not refreshed and self.settings.client_id:
                refreshed = True
                await self._get_token(force=True)
                continue

            if resp.status_code in RETRY_STATUSES and attempt < self.settings.max_retries:
                # 429s are rejected before processing, so retrying is safe for any
                # method. Other gateway errors are only retried for reads.
                if resp.status_code == 429 or method in SAFE_METHODS:
                    attempt += 1
                    await asyncio.sleep(_retry_after(resp) or _backoff(attempt, rate_limited=resp.status_code == 429))
                    continue

            if resp.status_code >= 400:
                raise _error_from_response(resp)

            if resp.status_code == 204 or not resp.content:
                return {"status": resp.status_code, "ok": True}
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                return resp.json()
            return {
                "status": resp.status_code,
                "content_type": ctype,
                "text": resp.text[:20000],
            }

    async def paginate(
        self,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        max_pages: int = 1,
    ) -> dict[str, Any]:
        """Follow ``page.next`` up to ``max_pages`` and merge ``data`` arrays."""
        items: list[Any] = []
        next_url: str | None = None
        pages = 0
        resp = await self.request("GET", path_or_url, params=params)
        while True:
            pages += 1
            if not isinstance(resp, dict) or "data" not in resp:
                return resp  # not a standard list response; hand it back untouched
            items.extend(resp.get("data") or [])
            next_url = (resp.get("page") or {}).get("next")
            if not next_url or pages >= max_pages:
                break
            resp = await self.request("GET", next_url)
        return {"data": items, "count": len(items), "pages_fetched": pages, "next": next_url}

    def _normalize(self, path_or_url: str) -> str:
        """Accept a relative API path or a full URL on the configured host."""
        if path_or_url.startswith(("http://", "https://")):
            parts = urlsplit(path_or_url)
            base = urlsplit(self.settings.base_url)
            if parts.netloc != base.netloc:
                raise RampError(0, f"Refusing to call host {parts.netloc!r}; only {base.netloc!r} is allowed.")
            path = parts.path + (f"?{parts.query}" if parts.query else "")
        else:
            path = path_or_url if path_or_url.startswith("/") else "/" + path_or_url
        if not path.startswith("/developer/v1/"):
            path = "/developer/v1" + path
        return path


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        else:
            out[k] = v
    return out


def _backoff(attempt: int, rate_limited: bool = False) -> float:
    base = 5.0 if rate_limited else 1.0
    return min(base * (2 ** (attempt - 1)), 60.0) + random.uniform(0, 0.5)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return min(float(raw), 90.0)
    except ValueError:
        return None


def _error_from_response(resp: httpx.Response, prefix: str = "Ramp API error") -> RampError:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:2000] or None
    msg = f"{prefix}: HTTP {resp.status_code}"
    if isinstance(body, dict):
        detail = body.get("error_v2") or body.get("error") or body.get("message")
        if isinstance(detail, dict):
            code = detail.get("error_code")
            text = detail.get("message") or detail.get("notes")
            if code or text:
                msg += f" ({code}: {text})" if code else f" ({text})"
        elif detail:
            msg += f" ({detail})"
    if resp.status_code == 403:
        msg += " - the token may be missing a required scope; check RAMP_SCOPES and your app's enabled scopes."
    return RampError(resp.status_code, msg, body, resp.headers.get("x-trace-id"))
