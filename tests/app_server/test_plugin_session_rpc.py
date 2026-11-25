"""The loopback channel a plugin's MCP server uses to enqueue a turn on its session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from vibe.app_server._mcp_auth import MCPAuthenticationService
from vibe.app_server._open_sessions import OpenSessions
from vibe.app_server._plugin_mcp import PluginMCPCatalog
from vibe.app_server._plugin_session_rpc import (
    RPC_HOST_ENV,
    RPC_PORT_ENV,
    RPC_TOKEN_ENV,
    SESSION_ID_ENV,
    PluginSessionRPC,
)
from vibe.app_server._runtime import merge_plugin_mcp_into_catalog
from vibe.app_server._session_backend_port import ResolvedMCPCatalog
from vibe.app_server.protocol import (
    SessionArchiveParams,
    SessionArchiveResponse,
    TurnEnqueueParams,
    TurnEnqueueResponse,
)
from vibe.core.config import MCPStdio
from vibe.core.plugins import PluginMCPServerDefinition
from vibe.core.tools.mcp.registry import MCPRegistry


class _FakeHost:
    """An OpenSessionHost that holds a fixed set of sessions."""

    def __init__(self, held: set[str]) -> None:
        self.held = held
        self.calls: list[TurnEnqueueParams] = []

    async def archive_open(
        self, params: SessionArchiveParams
    ) -> SessionArchiveResponse | None:
        return None

    async def enqueue_turn_open(
        self, params: TurnEnqueueParams
    ) -> TurnEnqueueResponse | None:
        if params.session_id not in self.held:
            return None
        self.calls.append(params)
        return TurnEnqueueResponse(queue_item_id="q1", waits=True)

    async def reload_config_files(self) -> None:
        return None

    def open_selection(self, session_id: str) -> tuple[str, str] | None:
        return None


def _enqueue_payload(token: str, session_id: str = "attacker", **extra: Any) -> dict:
    params: dict[str, Any] = {
        "sessionId": session_id,
        "entries": [
            {"role": "user", "content": [{"type": "text", "text": "peer mail"}]}
        ],
        **extra,
    }
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "session/turn/enqueue",
        "token": token,
        "params": params,
    }


async def _rpc(port: int, payload: dict | str) -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    writer.write(raw.encode() + b"\n")
    await writer.drain()
    line = await reader.readuntil(b"\n")
    writer.close()
    return json.loads(line)


@pytest.mark.asyncio
async def test_session_env_is_stable_per_session_and_revoked() -> None:
    service = PluginSessionRPC(OpenSessions())
    await service.ensure_started()
    try:
        env_a = service.session_env("s1")
        assert env_a[SESSION_ID_ENV] == "s1"
        assert env_a[RPC_HOST_ENV] == "127.0.0.1"
        assert int(env_a[RPC_PORT_ENV]) == service.port
        # Same session, same token: re-registering must not rotate it under a
        # running plugin process.
        assert service.session_env("s1")[RPC_TOKEN_ENV] == env_a[RPC_TOKEN_ENV]
        assert service.session_env("s2")[RPC_TOKEN_ENV] != env_a[RPC_TOKEN_ENV]
        service.revoke_session("s1")
        reply = await _rpc(service.port or 0, _enqueue_payload(env_a[RPC_TOKEN_ENV]))
        assert reply["error"]["code"] == -32001
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_enqueue_routes_to_the_token_session_and_strips_sender() -> None:
    service = PluginSessionRPC(OpenSessions())
    host = _FakeHost(held={"s1"})
    service._open_sessions.register(host)
    await service.ensure_started()
    try:
        env = service.session_env("s1")
        reply = await _rpc(
            service.port or 0,
            _enqueue_payload(
                env[RPC_TOKEN_ENV],
                session_id="some-other-session",
                sentBySession=True,
                sender={"sessionId": "evil", "title": "evil"},
                delivery="queue",
                hops=5,
                reply=True,
            ),
        )
        assert reply["id"] == 1
        assert reply["result"]["queueItemId"] == "q1"
        assert len(host.calls) == 1
        call = host.calls[0]
        # The token, not the request, picks the session.
        assert call.session_id == "s1"
        # The plugin cannot impersonate a session peer.
        assert not call.from_session
        assert call.delivery == "start_turn"
        assert call.hops == 1
        assert not call.reply
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unknown_token_and_foreign_method_are_rejected() -> None:
    service = PluginSessionRPC(OpenSessions())
    await service.ensure_started()
    try:
        reply = await _rpc(service.port or 0, _enqueue_payload("nope"))
        assert reply["error"]["code"] == -32001
        env = service.session_env("s1")
        reply = await _rpc(
            service.port or 0,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/stop",
                "token": env[RPC_TOKEN_ENV],
                "params": {},
            },
        )
        assert reply["error"]["code"] == -32601
        reply = await _rpc(service.port or 0, "not json")
        assert reply["error"]["code"] == -32700
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_enqueue_on_a_closed_session_fails_and_revokes_the_token() -> None:
    service = PluginSessionRPC(OpenSessions())
    service._open_sessions.register(_FakeHost(held=set()))
    await service.ensure_started()
    try:
        env = service.session_env("gone")
        reply = await _rpc(
            service.port or 0, _enqueue_payload(env[RPC_TOKEN_ENV], session_id="gone")
        )
        assert reply["error"]["code"] == -32002
        reply = await _rpc(
            service.port or 0, _enqueue_payload(env[RPC_TOKEN_ENV], session_id="gone")
        )
        assert reply["error"]["code"] == -32001
    finally:
        await service.close()


def _stdio_definition() -> PluginMCPServerDefinition:
    alias = "plugin_ab12_docs"
    return PluginMCPServerDefinition(
        plugin_name="docs",
        plugin_namespace="docs",
        source_id="docs",
        private_alias=alias,
        server=MCPStdio(
            name=alias,
            transport="stdio",
            command=["./server"],
            args=[],
            env={"PLUGIN_DATA": "/data/s1/docs"},
            cwd="/plugins/docs",
        ),
        config_file=Path("/plugins/docs/mcp.json"),
    )


@pytest.mark.asyncio
async def test_merge_injects_the_session_channel_into_stdio_plugin_servers() -> None:
    authentication = MCPAuthenticationService()
    catalog = PluginMCPCatalog(MCPRegistry(), authentication)
    await catalog.bind([_stdio_definition()])
    service = PluginSessionRPC(OpenSessions())
    await service.ensure_started()
    try:
        base = ResolvedMCPCatalog(revision="r1", servers=())
        merged = merge_plugin_mcp_into_catalog(
            base,
            catalog,
            authentication=authentication,
            session_id="s1",
            plugin_rpc=service,
        )
        assert len(merged.servers) == 1
        env = dict(merged.servers[0].env)
        # The plugin's own variables survive.
        assert env["PLUGIN_DATA"] == "/data/s1/docs"
        assert env[SESSION_ID_ENV] == "s1"
        assert env[RPC_HOST_ENV] == "127.0.0.1"
        assert int(env[RPC_PORT_ENV]) == service.port
        assert env[RPC_TOKEN_ENV]

        # Without a session the merge stays session-blind: no token leaks into
        # a catalog shared before the session id exists.
        plain = merge_plugin_mcp_into_catalog(
            base, catalog, authentication=authentication
        )
        assert RPC_TOKEN_ENV not in plain.servers[0].env
    finally:
        await service.close()
