# simple-game-client

> Status: design stage; no implementation yet.
> Scope: internal, project-specific MCP server.

## Overview

A minimal game-session client exposed through MCP tools. It keeps the network and session behavior required to connect an AI agent to a game server, without rendering, assets, input handling, or UI.

## Design Rationale

The MCP layer is a thin adapter. The project-specific work is reproducing the game's network stack from authorized client and server source:

- packet framing, opcodes, and message flow;
- connection, login, heartbeat, reconnect, and disconnect behavior;
- encryption, decryption, compression, and decompression where required;
- gateway discovery, server selection, ports, and zone routing.

The MCP protocol layer must use an official SDK (`@modelcontextprotocol/sdk` or the Python equivalent). “Hand-written” in this document refers only to the game protocol, crypto, and session layers. See [MCP-COMPLIANCE.md](./MCP-COMPLIANCE.md) for validation and internal handoff requirements.

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
- **MCP SDK**: official SDK required (specific package follows the language choice)
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

## Constraints and recorded findings

- **Prior-art check (2026-08)**: this repository's review of the awesome-mcp-servers Gaming category did not find an MCP server that connects to a live game server over a custom protocol while maintaining heartbeat and crypto. The review found game-data APIs, engine bridges, emulator control, and rules references instead.
- **Scope**: game protocols differ by title and client version, so each title needs its own implementation. This project is for internal team use and is not intended for public release. Handoff requirements are recorded in [MCP-COMPLIANCE.md](./MCP-COMPLIANCE.md).

_(design decisions, protocol docs, packet references, and source-code pointers go here)_
