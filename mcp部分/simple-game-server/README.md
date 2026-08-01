# simple-game-server

> Status: ❌ **evaluated and rejected** (2026-08) — kept as a decision record, no code planned.
> Companion discussion to [simple-game-client](../simple-game-client/README.md).

## The Idea (rejected)

A lightweight mock game server implementing only the protocol skeleton (login, heartbeat, a few core responses), so `simple-game-client` could be developed and tested without connecting to a real game server.

## Why Rejected

1. **Function-level fakes are strictly cheaper.** The actual goal is "let an AI agent operate the game", not "validate the TCP stack". Hooking the client's own send/receive dispatch (Lua/C# layer) simulates parameters and responses in the same language, at a fraction of the cost. A mock server instead forces you to implement both directions of every packet just to test one direction.
2. **Server emulation is a scope black hole.** Gateway → login → lobby → friends → leaderboards: each subsystem must fake believable responses, which amounts to rewriting the server. Effort/value ratio is terrible.
3. **Internal dev/test servers already exist.** In a game-studio environment, connecting to the internal dev server is both more realistic and zero-maintenance compared to a personal mock.

## Prior-art Check (2026-08)

- **Generic lightweight mock game servers**: none found (only trivial toy repos; generic mock tools like WireMock are HTTP-only and don't fit binary game protocols).
- **Game server emulators**: exist but are full private-server reimplementations for specific titles (e.g. rsmod for RuneScape, Edelstein for MapleStory) — heavy, version-locked, and legally gray. Structurally interesting (packet-handler registries, session management) but not the lightweight mock envisioned here.

## When It WOULD Be Worth Reconsidering

- No test environment is available (CI pipelines, or a third-party game whose server you don't control).
- Protocol conformance / fuzzing of the client network layer becomes a goal in itself (malformed packets, latency injection, reconnect storms) — things a real dev server won't do on demand.

If either case becomes real, revisit this document before designing.
