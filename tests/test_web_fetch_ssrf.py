"""web_fetch reaches only the public internet: no loopback, private network or metadata service."""

import ipaddress
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "coder" / "tools"))
import web_tools  # noqa: E402


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",  # shared address space
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost/",
        "file:///etc/passwd",
    ],
)
def test_internal_addresses_written_in_the_url_are_refused(url):
    assert web_tools._validate_url(url) is not None


def test_a_public_address_passes():
    assert web_tools._validate_url("https://93.184.216.34/") is None
    assert web_tools._is_public(ipaddress.ip_address("8.8.8.8"))


class FakeResolver:
    def __init__(self, address):
        self.address = address

    async def resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": self.address, "port": port, "family": 2, "proto": 0, "flags": 0}]

    async def close(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("address, allowed", [("127.0.0.1", False), ("169.254.169.254", False), ("10.1.2.3", False), ("93.184.216.34", True)])
async def test_a_name_is_resolved_only_to_public_addresses(address, allowed):
    resolver = web_tools.PublicResolver()
    resolver._resolver = FakeResolver(address)

    if allowed:
        assert (await resolver.resolve("example.test"))[0]["host"] == address
    else:
        with pytest.raises(OSError, match="internal address"):
            await resolver.resolve("inward.example.test")


@pytest.fixture
async def redirecting_server():
    """A server on loopback whose /to?url=... redirects there."""

    async def to(request):
        raise web.HTTPFound(request.query["url"])

    app = web.Application()
    app.router.add_get("/to", to)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["http://169.254.169.254/latest/meta-data/", "http://localhost:1/", "http://[::1]/"])
async def test_a_redirect_into_the_internal_network_is_refused(redirecting_server, target):
    # The first hop goes to loopback on purpose: web_fetch itself refuses that
    # URL, so _fetch_url is called directly to test the redirect alone.
    status, content, error = await web_tools._fetch_url(f"{redirecting_server}/to?url={target}", 5)

    assert status == 0 and content == ""
    assert error.startswith("Redirect refused")


@pytest.mark.asyncio
async def test_web_fetch_refuses_a_name_that_points_inward(monkeypatch):
    """End to end through the tool: the name resolves to loopback, nothing is fetched."""
    from types import SimpleNamespace

    monkeypatch.setattr(web_tools, "DefaultResolver", lambda: FakeResolver("127.0.0.1"))

    result = await web_tools.web_fetch.on_invoke_tool(
        SimpleNamespace(context=None, tool_name="web_fetch", tool_call_id="t"),
        '{"url": "http://inward.example.test/"}',
    )

    assert "internal address" in result and "127.0.0.1" in result
