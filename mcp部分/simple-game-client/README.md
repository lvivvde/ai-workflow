# simple-game-client

> Status: 🚧 placeholder — design stage, no code yet.
> A self-built MCP project (not a third-party index).

## Overview

A minimal game client exposed as an MCP server, letting AI agents connect to, inspect, and interact with a game session through standardized MCP tools.

## Design Rationale

A real game client is **heavy** — rendering, assets, input, UI — and holds a **persistent connection** to the server. This project strips all of that away: the MCP server itself *simulates* a client connection, keeping only the network/session layer.

Because there is no official protocol SDK, everything below the MCP interface must be implemented by hand, derived from the game's own client (and server) source code:

1. **Protocol parsing is reverse-engineered from source.** Packet formats, opcodes, and message flows are read from the game client's source code. Every packet sent and parsed by this MCP follows that implementation — there is no shortcut.
2. **Connection & heartbeat are self-maintained.** The MCP server owns the socket lifecycle: connect, login/handshake, keep-alive heartbeat at the interval the real client uses, reconnect with backoff, and graceful disconnect.
3. **Crypto layer is self-implemented.** Packets may be encrypted/decrypted and compressed/decompressed exactly as the real client does. The same algorithms, keys, and packet framing must be reproduced from source.
4. **Server endpoints are self-managed.** Gateway/login/server-list flows, port discovery, and zone routing are implemented manually based on the client/server source.

In short: **the MCP layer is thin; the hard part is faithfully reproducing the client's network stack.**

## Architecture (draft)

```
AI Agent (MCP client)
        │  MCP protocol (stdio / SSE)
        ▼
simple-game-client (MCP server)
   ├─ MCP tools layer        (thin: connect / get_state / send_action ...)
   ├─ session layer          (socket lifecycle, heartbeat, reconnect)
   ├─ protocol layer         (packet encode/decode, opcodes — from game source)
   └─ crypto layer           (encrypt/decrypt, compress/decompress — from game source)
        │  game protocol (TCP/UDP, ports from source)
        ▼
Game Server
```

- **Language**: TBD (TypeScript / Python / Go)
- **MCP SDK**: TBD
- **Transport**: stdio first, SSE/HTTP optional

## Planned MCP Tools

| Tool | Description | Status |
|---|---|---|
| `connect` | Full login flow: endpoint discovery, handshake, session establish | planned |
| `get_state` | Query current player/scene state (from parsed packets) | planned |
| `send_action` | Send a game action (move/attack/interact), protocol-encoded | planned |
| `heartbeat_status` | Inspect connection health, last heartbeat, latency | planned |
| `disconnect` | Graceful logout and socket close | planned |

## Roadmap

1. Read game client source: map protocol, crypto, heartbeat, and login flow
2. Implement protocol layer (encode/decode + opcode table)
3. Implement crypto layer (encryption/compression as per source)
4. Implement session layer (connect, heartbeat, reconnect)
5. Scaffold MCP server and expose tools
6. End-to-end test with an AI agent

## Notes

_(design decisions, protocol docs, packet references, and source-code pointers go here)_
