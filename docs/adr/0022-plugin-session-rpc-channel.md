# 0022 Plugin Session RPC Channel

## Decision

A plugin-owned stdio MCP server can enqueue a turn on the session it is bound
to, through one process-wide loopback listener — and nothing else.

- **One listener per process**, held by `ProcessServices` as
  `plugin_session_rpc` ([0019](0019-process-services.md)), bound to
  `127.0.0.1` on an ephemeral port, newline-delimited JSON-RPC 2.0. It is
  started lazily, only when a session binds plugin MCP servers.
- **Per-session tokens.** Each plugin stdio server's `env` gains
  `VIBE_SESSION_ID`, `VIBE_SESSION_RPC_HOST`, `VIBE_SESSION_RPC_PORT` and
  `VIBE_SESSION_RPC_TOKEN`. The token is random per session, names exactly
  that session, and is revoked when the session closes. The env is injected
  both into the MCP catalog handed to the runtime and into a persistent
  app-server-side connection the provider keeps open for the session's life
  (`UnifiedPluginProvider._start_plugin_session_rpc`): the runtime opens
  stdio MCP processes only while it uses them, so an idle session would
  otherwise have no plugin process alive to do the enqueueing.
- **One method.** The listener accepts only `session/turn/enqueue`, routed
  through `OpenSessions` to the host holding the session. The token picks the
  session: any `sessionId` in the params is overridden, and `sender` /
  `sentBySession` / `delivery` / `hops` / `reply` are stripped, so a plugin
  can never impersonate a peer session. Requests are size-capped.
- **Untrusted content.** An enqueued message is an ordinary user message. It
  can start a turn on an idle session but cannot approve a permission, change
  configuration, or run a slash command. Plugin hooks remain the mechanism for
  content delivered at turn start (`pre_agent`).

## Rationale

Issue #1422: an idle session never starts a turn, so plugin hooks never fire
and a plugin's MCP server had no channel to the app server's
`session/turn/enqueue`. The MCP server is the right writer — it is long-lived
and owns its data source — so the channel is scoped to the one write it needs,
rather than exposing the app-server protocol to plugin processes. The
alternative of an MCP server-to-client request over the plugin's own MCP
connection was rejected: it needs runtime (harness) support, which is a pinned
external dependency, and a custom MCP method is a second, undocumented
invocation mechanism.

## Agent Guidance

- Keep the allowlist at exactly `session/turn/enqueue`; any new plugin-facing
  method is a new decision, not an increment of this one.
- Never hand the token to anything but the session's plugin stdio servers'
  `env`; inject it in `merge_plugin_mcp_into_catalog` only when a `session_id`
  is known, so a shared pre-session catalog carries no token.
- Route enqueues through `OpenSessions.enqueue_turn`, never by holding a
  session backend directly; a token for a closed session fails and is revoked.

## Flag To User When

- A plugin needs any app-server capability beyond enqueueing a turn on its own
  session (approvals, config, slash commands): that is a new invocation
  mechanism and must be flagged per [0007](0007-extension-mechanisms.md), not
  grown as a side channel.
