import asyncio
import json
import time
from types import SimpleNamespace

import pytest


class FakeQuery:
    def __init__(self, servers):
        self._servers = servers

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._servers


class FakeDB:
    def __init__(self, servers):
        self._servers = servers

    def query(self, *_args, **_kwargs):
        return FakeQuery(self._servers)

    def close(self):
        pass


@pytest.mark.asyncio
async def test_connect_all_enabled_runs_concurrently(monkeypatch):
    from src.mcp_manager import McpManager

    manager = McpManager()

    servers = [
        SimpleNamespace(
            id=1,
            name="server1",
            transport="stdio",
            command="cmd1",
            args=json.dumps([]),
            env=json.dumps({}),
            url=None,
        ),
        SimpleNamespace(
            id=2,
            name="server2",
            transport="stdio",
            command="cmd2",
            args=json.dumps([]),
            env=json.dumps({}),
            url=None,
        ),
        SimpleNamespace(
            id=3,
            name="server3",
            transport="stdio",
            command="cmd3",
            args=json.dumps([]),
            env=json.dumps({}),
            url=None,
        ),
    ]

    # Patch the SessionLocal used by connect_all_enabled().
    import src.mcp_manager as mcp_manager

    monkeypatch.setattr(
        mcp_manager,
        "SessionLocal",
        lambda: FakeDB(servers),
    )

    async def fake_connect_with_timeout(_server):
        await asyncio.sleep(1)

    # We're testing that connect_all_enabled launches these concurrently,
    # not the implementation of connect_server().
    monkeypatch.setattr(
        manager,
        "_connect_with_timeout",
        fake_connect_with_timeout,
    )

    start = time.perf_counter()

    await manager.connect_all_enabled()

    elapsed = time.perf_counter() - start

    # Sequential would take ~3 seconds.
    # Concurrent should take about 1 second.
    assert 0.9 <= elapsed < 2.0

@pytest.mark.asyncio
async def test_connect_all_enabled_timeout_does_not_block_other_servers(monkeypatch):
    import src.mcp_manager as mcp_manager
    from src.mcp_manager import McpManager

    manager = McpManager()

    servers = [
        SimpleNamespace(
            id=1,
            name="fast1",
            transport="stdio",
            command="cmd1",
            args=json.dumps([]),
            env=json.dumps({}),
            url=None,
        ),
        SimpleNamespace(
            id=2,
            name="slow",
            transport="stdio",
            command="cmd2",
            args=json.dumps([]),
            env=json.dumps({}),
            url=None,
        ),
        SimpleNamespace(
            id=3,
            name="fast2",
            transport="stdio",
            command="cmd3",
            args=json.dumps([]),
            env=json.dumps({}),
            url=None,
        ),
    ]

    monkeypatch.setattr(
        mcp_manager,
        "SessionLocal",
        lambda: FakeDB(servers),
    )

    completed = []

    async def fake_connect_server(server_id, **kwargs):
        if server_id == 2:
            # Simulate a hung connection.
            await asyncio.sleep(30)
        else:
            await asyncio.sleep(0.1)
            completed.append(server_id)

    monkeypatch.setattr(
        manager,
        "connect_server",
        fake_connect_server,
    )

    #
    # Don't actually wait 20 seconds during the test.
    # Replace asyncio.wait_for used by mcp_manager with a much shorter timeout.
    #
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.2)

    monkeypatch.setattr(
        mcp_manager.asyncio,
        "wait_for",
        short_wait_for,
    )

    start = time.perf_counter()

    await manager.connect_all_enabled()

    elapsed = time.perf_counter() - start

    assert set(completed) == {1, 3}
    assert elapsed < 1

def _slow_then_fast(manager, calls, hangs=1):
    async def fake_connect_server(server_id, **kwargs):
        calls.append(server_id)
        if len(calls) <= hangs:
            await asyncio.sleep(30)
        manager._connections[server_id] = {"status": "connected", "name": kwargs["name"]}
        return True
    return fake_connect_server


def _short_timeouts(monkeypatch, mcp_manager):
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.2)

    monkeypatch.setattr(mcp_manager.asyncio, "wait_for", short_wait_for)


_SLOW = SimpleNamespace(id="slow", name="slow", transport="stdio", command="cmd",
                        args=json.dumps([]), env=json.dumps({}), url=None)


@pytest.mark.asyncio
async def test_timed_out_server_is_retried_in_the_background(monkeypatch):
    import src.mcp_manager as mcp_manager
    from src.mcp_manager import McpManager

    manager = McpManager()
    manager.RETRY_DELAYS = (0, 0, 0)
    monkeypatch.setattr(mcp_manager, "SessionLocal", lambda: FakeDB([_SLOW]))
    _short_timeouts(monkeypatch, mcp_manager)
    calls = []
    monkeypatch.setattr(manager, "connect_server", _slow_then_fast(manager, calls, hangs=2))

    start = time.perf_counter()
    await manager.connect_all_enabled()
    # Startup does not wait for the retries.
    assert time.perf_counter() - start < 1
    assert manager._connections["slow"]["status"] == "timeout"

    await manager._retry_tasks["slow"]
    # One startup attempt, one retry that timed out again, one that connected; no further tries.
    assert calls == ["slow", "slow", "slow"]
    assert manager._connections["slow"]["status"] == "connected"
    assert "slow" not in manager._retry_tasks


@pytest.mark.asyncio
async def test_retry_stops_when_the_server_is_disconnected(monkeypatch):
    import src.mcp_manager as mcp_manager
    from src.mcp_manager import McpManager

    manager = McpManager()
    manager.RETRY_DELAYS = (5,)
    monkeypatch.setattr(mcp_manager, "SessionLocal", lambda: FakeDB([_SLOW]))
    _short_timeouts(monkeypatch, mcp_manager)
    calls = []
    monkeypatch.setattr(manager, "connect_server", _slow_then_fast(manager, calls))

    await manager.connect_all_enabled()
    retry = manager._retry_tasks["slow"]
    await manager.disconnect_server("slow")
    await asyncio.sleep(0)

    assert retry.cancelled()
    assert calls == ["slow"]
    assert "slow" not in manager._connections
