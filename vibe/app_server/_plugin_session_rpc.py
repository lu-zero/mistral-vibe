"""Loopback RPC channel from a plugin's MCP server to the session it is bound to.

A plugin-owned stdio MCP server runs for the life of its session, but an idle
session never starts a turn, so a hook (``post_tool`` included) never fires to
surface what the plugin learned. This service closes that gap with the smallest
possible surface: a localhost TCP listener, started once per process, that
accepts newline-delimited JSON-RPC 2.0 requests authenticated by a per-session
token, and allows exactly one method — ``session/turn/enqueue`` — on the
session the token was issued for. The enqueued content is untrusted data: it
becomes an ordinary user message, so it can never approve a permission, change
configuration, or run a slash command.

The session's plugin MCP servers learn the channel through their environment
(``VIBE_SESSION_ID``, ``VIBE_SESSION_RPC_HOST``, ``VIBE_SESSION_RPC_PORT``,
``VIBE_SESSION_RPC_TOKEN``), injected when the session's MCP catalog is built.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from pydantic import ValidationError

from vibe.app_server._model import validate_wire
from vibe.app_server._open_sessions import OpenSessions
from vibe.app_server._session_backend_port import SessionBackendError
from vibe.app_server.protocol import TurnEnqueueParams, TurnEnqueueResponse

logger = logging.getLogger(__name__)

# Environment variable names handed to plugin-owned stdio MCP servers.
SESSION_ID_ENV = "VIBE_SESSION_ID"
RPC_HOST_ENV = "VIBE_SESSION_RPC_HOST"
RPC_PORT_ENV = "VIBE_SESSION_RPC_PORT"
RPC_TOKEN_ENV = "VIBE_SESSION_RPC_TOKEN"

_ENQUEUE_METHOD = "session/turn/enqueue"
_RPC_HOST = "127.0.0.1"
# One request line may carry a whole turn's content; the cap is a guardrail
# against a runaway plugin, not a quota.
_MAX_REQUEST_BYTES = 1_000_000
_MAX_CONNECTIONS = 16
# JSON-RPC reserved / application error codes used on this channel.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_AUTH_FAILED = -32001
_SESSION_NOT_FOUND = -32002
_INTERNAL = -32603


class PluginSessionRPC:
    """Process-wide loopback listener that routes plugin enqueues to sessions.

    One instance lives in ``ProcessServices``. Tokens are random per session
    and revoked when the session closes; a token only ever names one session,
    and the listener only ever performs ``session/turn/enqueue`` on it.
    """

    def __init__(self, open_sessions: OpenSessions) -> None:
        self._open_sessions = open_sessions
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._tokens: dict[str, str] = {}  # token -> session_id
        self._by_session: dict[str, str] = {}  # session_id -> token
        self._closing = False

    @property
    def port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    async def ensure_started(self) -> None:
        """Start the listener once; idempotent and safe to call often."""
        if self._server is not None or self._closing:
            return
        self._server = await asyncio.start_server(
            self._handle, _RPC_HOST, 0, limit=_MAX_REQUEST_BYTES + 16
        )
        logger.info("plugin session RPC listening on %s:%s", _RPC_HOST, self.port)

    def session_env(self, session_id: str) -> dict[str, str]:
        """The environment naming this session's channel, with a fresh token."""
        token = self._by_session.get(session_id)
        if token is None or self._tokens.get(token) != session_id:
            token = secrets.token_urlsafe(32)
            old = self._by_session.get(session_id)
            if old is not None:
                self._tokens.pop(old, None)
            self._by_session[session_id] = token
            self._tokens[token] = session_id
        return {
            SESSION_ID_ENV: session_id,
            RPC_HOST_ENV: _RPC_HOST,
            RPC_PORT_ENV: str(self.port or 0),
            RPC_TOKEN_ENV: token,
        }

    def revoke_session(self, session_id: str) -> None:
        token = self._by_session.pop(session_id, None)
        if token is not None:
            self._tokens.pop(token, None)

    async def close(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in self._connections:
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        self._tokens.clear()
        self._by_session.clear()

    async def enqueue(self, token: str, params: dict[str, Any]) -> TurnEnqueueResponse:
        """Route one enqueue. The token picks the session; the caller cannot."""
        session_id = self._tokens.get(token)
        if session_id is None:
            raise _RpcError(_AUTH_FAILED, "unknown or revoked token")
        # The plugin names no session and no sender: the token's session is
        # the target, and the message is a plain user message. Strip both the
        # wire aliases and the field names, which the wire model also accepts.
        params = {
            key: value
            for key, value in params.items()
            if key
            not in {
                "sessionId",
                "session_id",
                "sentBySession",
                "sent_by_session",
                "sender",
                "delivery",
                "hops",
                "reply",
            }
        }
        params["sessionId"] = session_id
        try:
            parsed = validate_wire(TurnEnqueueParams, params)
        except ValidationError as exc:
            raise _RpcError(
                _INVALID_PARAMS, f"invalid params: {exc.errors(include_url=False)}"
            ) from exc
        response = await self._open_sessions.enqueue_turn(parsed)
        if response is None:
            # The session closed after the token was issued: drop the token.
            self.revoke_session(session_id)
            raise _RpcError(_SESSION_NOT_FOUND, "session is not open")
        return response

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is None or self._closing or len(self._connections) >= _MAX_CONNECTIONS:
            writer.close()
            return
        self._connections.add(task)
        try:
            while not self._closing:
                try:
                    line = await reader.readuntil(b"\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    break
                reply = await self._handle_line(line)
                writer.write(reply + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._connections.discard(task)
            writer.close()

    async def _handle_line(self, line: bytes) -> bytes:
        request_id, error, response = await self._dispatch_line(line)
        if error is not None:
            return _error(request_id, error[0], error[1], data=error[2])
        assert response is not None
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": response.model_dump(mode="json", by_alias=True),
            },
            separators=(",", ":"),
        ).encode()

    async def _dispatch_line(
        self, line: bytes
    ) -> tuple[Any, tuple[int, str, Any] | None, TurnEnqueueResponse | None]:
        """One request line: the id, an error triple, or the enqueue response."""
        request_id, token, params, error = _parse_request(line)
        if error is not None or token is None or params is None:
            return request_id, error, None
        try:
            response = await self.enqueue(token, params)
        except _RpcError as exc:
            return request_id, (exc.code, exc.message, None), None
        except SessionBackendError as exc:
            return (
                request_id,
                (_INTERNAL, str(exc), {"protocolCode": exc.code.value}),
                None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("plugin session RPC enqueue failed", exc_info=True)
            return request_id, (_INTERNAL, f"{type(exc).__name__}: {exc}", None), None
        return request_id, None, response


def _parse_request(
    line: bytes,
) -> tuple[Any, str | None, dict[str, Any] | None, tuple[int, str, Any] | None]:
    """Validate the envelope: id, the single allowed method, token, params."""
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None, None, (_PARSE_ERROR, "parse error", None)
    if not isinstance(request, dict) or "id" not in request:
        return (
            None,
            None,
            None,
            (_INVALID_REQUEST, "request must be an object with an id", None),
        )
    request_id = request["id"]
    if request.get("method") != _ENQUEUE_METHOD:
        return (
            request_id,
            None,
            None,
            (_METHOD_NOT_FOUND, "only session/turn/enqueue is allowed", None),
        )
    token, params = request.get("token"), request.get("params")
    if not isinstance(token, str) or not isinstance(params, dict):
        return (
            request_id,
            None,
            None,
            (_INVALID_REQUEST, "token and params are required", None),
        )
    return request_id, token, params, None


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: Any, code: int, message: str, data: Any = None) -> bytes:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "error": error}, separators=(",", ":")
    ).encode()


__all__ = [
    "RPC_HOST_ENV",
    "RPC_PORT_ENV",
    "RPC_TOKEN_ENV",
    "SESSION_ID_ENV",
    "PluginSessionRPC",
]
