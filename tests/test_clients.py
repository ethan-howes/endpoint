"""Tests for the §4.5 guarantee: no service failure may break a ride.

ENDPOINT.md §4.5 is the single most load-bearing requirement in the orchestrator:
"If anything fails or returns nothing, fall back to the nearest legal spot and log
why." These tests pin the *distinctions* that requirement depends on, because
the failure mode is not an exception -- it is a fallback that fires for the wrong
reason and tells the rider something untrue.

The distinction that matters: a service that answered with "nothing here" is not
a service that could not be asked. The first is a legitimate answer; the second is
a degradation. Collapsing them turns a 504 on S1 into "no legal spots near you",
which is a confident lie delivered in the shape of data.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from orchestrator.clients import ServiceResult, call_service, merge_fallbacks


pytestmark = pytest.mark.anyio


class _Payload(BaseModel):
    value: int


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #

class TestSuccess:
    async def test_returns_parsed_model(self):
        c = _client(lambda r: httpx.Response(200, json={"value": 7}))
        r = await call_service("S1", "GET", "/x", model=_Payload, client=c)
        assert r.ok is True
        assert r.data == _Payload(value=7)

    async def test_returns_raw_dict_when_no_model(self):
        c = _client(lambda r: httpx.Response(200, json={"a": 1}))
        r = await call_service("S1", "GET", "/x", client=c)
        assert r.ok is True and r.data == {"a": 1}

    async def test_a_valid_empty_list_is_a_success_not_a_failure(self):
        """The distinction the whole module exists for. S1 answering
        `{"spots": []}` is a real answer: there is genuinely nothing there, and
        the caller is entitled to say so rather than to report a degradation."""
        c = _client(lambda r: httpx.Response(200, json={"spots": []}))
        r = await call_service("S1", "POST", "/spots/legal", client=c)
        assert r.ok is True
        assert r.data == {"spots": []}
        assert r.reason == ""


# --------------------------------------------------------------------------- #
# Failure
# --------------------------------------------------------------------------- #

class TestFailure:
    async def test_5xx_is_a_failure_with_the_status_in_the_reason(self):
        c = _client(lambda r: httpx.Response(503, text="overloaded"))
        r = await call_service("S1", "GET", "/x", client=c)
        assert r.ok is False
        assert "503" in r.reason

    async def test_4xx_is_a_failure_and_is_logged(self, caplog):
        """A 4xx is our bug, not the service's -- it will not fix itself, so it
        deserves a louder log than a 5xx."""
        c = _client(lambda r: httpx.Response(422, text="bad radius"))
        r = await call_service("S1", "POST", "/spots/legal", client=c)
        assert r.ok is False
        assert "422" in r.reason
        assert any(rec.levelname == "WARNING" for rec in caplog.records)

    async def test_timeout_is_reported_as_a_timeout_not_as_unreachable(self):
        """REGRESSION. `httpx.TimeoutException` is not a builtin `TimeoutError`,
        so an `except TimeoutError` clause is dead code and a timeout falls
        through to the generic HTTPError handler -- reported as "unreachable".
        The two send you to completely different problems."""
        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        r = await call_service("S1", "GET", "/x", client=_client(_raise))
        assert r.ok is False
        assert "timed out" in r.reason
        assert "unreachable" not in r.reason

    async def test_connection_refused_is_reported_as_unreachable(self):
        def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        r = await call_service("S1", "GET", "/x", client=_client(_raise))
        assert r.ok is False
        assert "unreachable" in r.reason

    async def test_contract_mismatch_degrades_instead_of_raising(self):
        """A shape change upstream must not surface as a KeyError deep in fusion
        logic at demo time. It surfaces here, as a recorded fallback."""
        c = _client(lambda r: httpx.Response(200, json={"unexpected": True}))
        r = await call_service("S1", "GET", "/x", model=_Payload, client=c)
        assert r.ok is False
        assert "contract" in r.reason

    async def test_non_json_body_degrades_instead_of_raising(self):
        """A proxy returning an HTML error page with a 200 status is a real
        failure mode, and json.loads is where it would otherwise blow up."""
        c = _client(lambda r: httpx.Response(200, text="<html>gateway</html>"))
        r = await call_service("S1", "GET", "/x", client=c)
        assert r.ok is False

    async def test_a_bug_in_the_caller_does_not_escape(self):
        """§4.5's last line. An exception in our own code must still come back as
        a ServiceResult, not as a 500 on the ride endpoint."""
        def _raise(request):
            raise ValueError("a bug in the caller")

        r = await call_service("S1", "GET", "/x", client=_client(_raise))
        assert r.ok is False
        assert "failed" in r.reason

    async def test_never_raises_on_any_of_the_above(self):
        """The contract, stated as a property: the function's signature says it
        returns a result, so it must not raise."""
        handlers = {
            "5xx": lambda r: httpx.Response(500),
            "4xx": lambda r: httpx.Response(400),
            "timeout": lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("x")),
            "garbage": lambda r: httpx.Response(200, text="not json"),
        }
        for name, h in handlers.items():
            r = await call_service("S1", "GET", "/x", client=_client(h))
            assert isinstance(r.ok, bool)
            if not r.ok:
                assert r.reason, f"{name} failed without saying why"


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #

class TestTimeouts:
    @pytest.mark.parametrize(
        "service,budget", [("S1", 3.0), ("S2", 6.0), ("S3", 8.0)]
    )
    def test_each_service_gets_its_own_section_4_5_budget(self, service, budget):
        """§4.5: S1 3s, S2 6s, S3 8s. Enforced here because these are the
        *caller's* budgets -- a service cannot time itself out on the caller's
        behalf in a way the caller can observe."""
        from orchestrator.clients import _budget

        assert _budget(service) == budget

    def test_an_unknown_service_name_is_a_programming_error(self):
        """A typo in a service name must fail loudly at the call site, not
        silently get a default budget and a wrong URL."""
        from orchestrator.clients import _budget, base_url

        with pytest.raises(KeyError):
            _budget("S4")
        with pytest.raises(KeyError):
            base_url("S4")


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #

class TestMergeFallbacks:
    def test_records_each_degradation_in_order(self):
        out: list[str] = []
        merge_fallbacks(out, ServiceResult(ok=False, reason="S1 timed out after 3s"))
        merge_fallbacks(out, ServiceResult(ok=False, reason="S2 unreachable"))
        assert out == ["S1 timed out after 3s", "S2 unreachable"]

    def test_does_not_duplicate(self):
        out: list[str] = []
        r = ServiceResult(ok=False, reason="S1 timed out after 3s")
        merge_fallbacks(out, r)
        merge_fallbacks(out, r)
        assert out == ["S1 timed out after 3s"]

    def test_ignores_successes(self):
        out: list[str] = []
        merge_fallbacks(out, ServiceResult(ok=True, data={}, reason="ignored"))
        assert out == []

    def test_fallbacks_survive_into_the_response_shape(self):
        """The reasons have to be readable as a list of strings, because the
        response model types them that way."""
        out: list[str] = []
        merge_fallbacks(out, ServiceResult(ok=False, reason="a"))
        assert all(isinstance(s, str) for s in out)
        json.dumps(out)  # must be serializable as-is
