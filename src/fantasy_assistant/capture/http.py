"""Shared HTTP discipline for every collector.

One place for retries, backoff, and status checking — individual collectors
were calling .json() on unchecked responses with no retry, so one transient
5xx or timeout silently discarded a whole batch. gather_ok() replaces bare
asyncio.gather so one failed task can no longer take down its siblings
undetected: failures are counted, logged, and returned separately.
"""
from __future__ import annotations

import asyncio
import time

import httpx

RETRIES = 3
BACKOFF_S = 1.5


def get_json(url: str, params: dict | None = None, timeout: float = 30,
             client: httpx.Client | None = None) -> dict | list:
    """GET with retries + raise_for_status. Raises after RETRIES attempts."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            if client is not None:
                r = client.get(url, params=params)
            else:
                r = httpx.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # httpx transport + status + decode errors
            last = exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_S * (attempt + 1))
    raise last  # type: ignore[misc]


async def aget_json(client: httpx.AsyncClient, url: str,
                    params: dict | None = None) -> dict | list:
    """Async GET with retries + raise_for_status."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < RETRIES - 1:
                await asyncio.sleep(BACKOFF_S * (attempt + 1))
    raise last  # type: ignore[misc]


async def gather_ok(coros, label: str = "batch") -> tuple[list, int]:
    """gather with exception isolation: -> (successful results, n_failed).

    Failures are printed (they land in the bus/routine logs) instead of
    either crashing the batch or vanishing.
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    ok, failed = [], 0
    for res in results:
        if isinstance(res, BaseException):
            failed += 1
        else:
            ok.append(res)
    if failed:
        print(f"[http] {label}: {failed}/{len(results)} tasks failed "
              f"(retries exhausted) — batch continues with the rest", flush=True)
    return ok, failed
