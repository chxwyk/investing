"""Dependency-light check for RPC pacing and 429 retry behavior."""

from __future__ import annotations

import asyncio
import json
import sys
import types


class FakeClientError(Exception):
    pass


class FakeClientTimeout:
    def __init__(self, *, total: int) -> None:
        self.total = total


fake_aiohttp = types.ModuleType("aiohttp")
fake_aiohttp.ClientError = FakeClientError
fake_aiohttp.ClientTimeout = FakeClientTimeout
fake_aiohttp.ClientSession = object
sys.modules.setdefault("aiohttp", fake_aiohttp)

from smart_money_bot.rpc import SolanaRPC  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = json.dumps(body)
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return self._body


class FakeRequestContext:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeSession:
    closed = False

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def post(self, _url: str, *, json: dict) -> FakeRequestContext:
        assert json["method"] == "getHealth"
        response = self.responses[self.calls]
        self.calls += 1
        return FakeRequestContext(response)


async def main() -> None:
    session = FakeSession(
        [
            FakeResponse(429, {"error": {"code": 429, "message": "rate limited"}}),
            FakeResponse(200, {"result": "ok"}),
        ]
    )
    rpc = SolanaRPC(
        "https://example.invalid",
        max_requests_per_second=1_000_000,
        max_retries=1,
    )
    rpc._session = session  # type: ignore[assignment]
    rpc._retry_delay = lambda *_args, **_kwargs: 0.0  # type: ignore[method-assign]
    assert await rpc.health() == "ok"
    assert session.calls == 2
    print("RPC SELF-CHECK PASSED: HTTP 429 retried successfully")


if __name__ == "__main__":
    asyncio.run(main())
