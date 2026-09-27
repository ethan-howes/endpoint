"""Calling S1/S2/S3 with §4.5's guarantee enforced in one place.

ENDPOINT.md section 4.5 states the rule plainly: "No service failure should break
a ride. If anything fails or returns nothing, fall back to the nearest legal spot
and log why." The doc's own pseudocode then says to do that at each call site.

Doing it at each call site is how one of them gets forgotten, and the one that
gets forgotten is the one that matters. So every outbound call goes through
``call_service`` here, which owns all three obligations at once:

  * **the timeout** -- S1 3s, S2 6s, S3 8s. These are the *caller's* budgets, so
    they are the caller's to enforce; a service cannot time itself out on its
    behalf in a way the caller can see.
  * **the fallback** -- returns ``None`` instead of raising, so a caller cannot
    accidentally treat "service down" as "no data exists".
  * **the bookkeeping** -- records why into the ride's ``fallbacks_used``, which
    is what makes a degraded ride explainable afterwards instead of merely
    quietly wrong.

The distinction that matters most is the last one. ``None`` from S1 means "we
could not ask", which is different from "we asked and there is nothing there".
The first is a fallback; the second is a legitimate empty answer. Collapsing them
means a 504 on S1 silently becomes 'no legal spots near you', which is a
confident lie delivered in the shape of data.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from shared.config import SETTINGS

log = logging.getLogger("endpoint.orchestrator.clients")

T = TypeVar("T", bound=BaseModel)


class ServiceResult(BaseModel):
    """One service call's outcome, including the failure.

    Kept as a value rather than a bare ``None`` because the caller needs to say
    *which* service degraded when it writes the rider's message, and because the
    distinction between "asked, nothing there" and "could not ask" has to
    survive all the way to the response.
    """

    ok: bool
    data: Any | None = None
    #: Human-readable reason, appended to the ride's ``fallbacks_used``.
    reason: str = ""
    #: True when the service answered but had nothing to say. NOT a failure.
    empty: bool = False
    elapsed_ms: int = 0


def _budget(service: str) -> float:
    return {
        "S1": SETTINGS.timeout_s1_s,
        "S2": SETTINGS.timeout_s2_s,
        "S3": SETTINGS.timeout_s3_s,
    }[service]


def base_url(service: str) -> str:
    return {"S1": SETTINGS.s1_url, "S2": SETTINGS.s2_url, "S3": SETTINGS.s3_url}[service]


async def call_service(
    service: str,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    model: type[T] | None = None,
    client: httpx.AsyncClient | None = None,
) -> ServiceResult:
    """One timeout-guarded, never-raising service call.

    ``model`` is a Pydantic model class to validate the response into. Validating
    is not ceremony: the orchestrator is the only thing that sees S1/S2/S3
    responses, so a shape change upstream would otherwise surface as a
    ``KeyError`` deep inside fusion logic at demo time, instead of as a logged
    fallback at the boundary.
    """
    budget = _budget(service)
    url = f"{base_url(service).rstrip('/')}{path}"
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=budget)

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        resp = await client.request(method, url, json=json_body, params=params)
        elapsed = int((loop.time() - started) * 1000)

        if resp.status_code >= 500:
            return ServiceResult(
                ok=False, reason=f"{service} {path} -> HTTP {resp.status_code}",
                elapsed_ms=elapsed,
            )
        if resp.status_code >= 400:
            # A 4xx is our bug (bad request shape), not the service's. Worth a
            # louder log because it will not fix itself.
            log.warning("%s %s -> HTTP %s: %s", service, path, resp.status_code,
                        resp.text[:200])
            return ServiceResult(
                ok=False, reason=f"{service} rejected the request ({resp.status_code})",
                elapsed_ms=elapsed,
            )

        payload = resp.json()
        if model is not None:
            try:
                payload = model.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                return ServiceResult(
                    ok=False,
                    reason=f"{service} response did not match the contract: {type(exc).__name__}",
                    elapsed_ms=elapsed,
                )
        return ServiceResult(ok=True, data=payload, elapsed_ms=elapsed)
    except httpx.TimeoutException:
        # Must be caught before the HTTPError clause: httpx.TimeoutException is a
        # subclass of HTTPError, and it is NOT a builtin TimeoutError, so a naive
        # `except TimeoutError` is dead code and every timeout gets reported as
        # "unreachable" -- which sends you looking at the wrong failure. A timeout
        # means the service is over budget, which is a different fix than a
        # service that is not running.
        return ServiceResult(ok=False, reason=f"{service} timed out after {budget:g}s")
    except httpx.HTTPError as exc:
        return ServiceResult(ok=False, reason=f"{service} unreachable ({type(exc).__name__})")
    except Exception as exc:  # noqa: BLE001
        # Last line of §4.5. A bug in this process must not become a dead ride.
        log.exception("unexpected error calling %s %s", service, path)
        return ServiceResult(ok=False, reason=f"{service} call failed ({type(exc).__name__})")
    finally:
        if owns_client:
            await client.aclose()


def merge_fallbacks(ride_fallbacks: list[str], *results: ServiceResult) -> list[str]:
    """Accumulate the reasons a set of calls degraded, in order, without dupes.

    The rider-facing message may be the only artifact anyone looks at after a
    rough demo, so the degradation reasons have to survive into the response.
    """
    for r in results:
        if not r.ok and r.reason and r.reason not in ride_fallbacks:
            ride_fallbacks.append(r.reason)
    return ride_fallbacks
