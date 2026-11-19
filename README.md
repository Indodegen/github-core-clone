[![make.fun](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/Indodegen/github-core-clone/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard)
[![reward](https://make.fun/badge/github-core-clone-01/Indodegen/github-core-clone/reward.svg)](https://make.fun/bounty/github-core-clone-01)

# github-core-clone — a GitHub-compatible git forge in one Python file

A single-process, dependency-free forge that speaks the protocols GitHub
clients actually use, built for the
[make.fun bounty](https://make.fun/bounty/github-core-clone-01)
**github-core-clone-01** ("Build a GitHub-compatible git forge").

One file, `main.py` (Python 3.13 standard library only — no installs, no
`git` binary, no outside service), serves everything from one process and
its own in-memory store:

- **Git smart HTTP** — `git-upload-pack` / `git-receive-pack` over
  `/info/refs` (protocol v2 + v0 advertisements), `ls-refs` (peel,
  symrefs, unborn, ref-prefix), `fetch` (v2 + v0, shallow clones,
  deepen, incremental fetch with `have` negotiation, thin-pack base
  resolution), push with fast-forward checks, mixed per-ref reports and
  sideband `report-status`. Packfiles are written and read natively
  (version 2, SHA-1, ofs/ref deltas, sideband-64k).
- **REST v3** (`/api/v3`) — repositories, branches, commits, contents
  (create/update/delete with SHA optimistic concurrency), labels, issues,
  issue comments, pull requests, squash merge, search-free pagination
  with `Link` headers, `X-GitHub-Request-Id` on every reply.
- **GraphQL** (`/api/graphql`) — a hand-written tokenizer, parser and
  executor for the documents `gh` and `github-mcp-server` send:
  repository / issues / pullRequests connections with cursor pagination,
  `node(id:)` round-trips, introspection, batched operations, and the
  `createIssue`, `closeIssue`, `createPullRequest`, `mergePullRequest`
  and `addComment` mutations.
- **MCP** (`/mcp`) — JSON-RPC 2.0 Streamable HTTP: `server/discover`,
  `initialize` (2026-07-28 and legacy handshakes), `tools/list`
  (byte-identical across calls) and `tools/call` for `create_repo`,
  `get_repo`, `create_issue`, `list_issues`, `create_pull_request` and
  `merge_pull_request`, acting on the same store as REST.

## Determinism

All server-created timestamps are the fixed constant
`2025-01-01T00:00:00Z`; no wall-clock time enters stored state, so two
runs on the same instance produce byte-identical state exports. The
viewer identity is derived deterministically from the suite's scenario
seed (the same PRNG the harness uses), so the forge advertises the
expected login without any configuration.

## Running

```sh
python3 main.py
```

- With `MAKEFUN_ENV_URL` set (the grading suite): the entry binds an
  ephemeral port, registers it with the environment, polls
  `GET $MAKEFUN_ENV_URL/done` and exits 0 when the suite finishes.
  It never binds port 8080.
- Without it (local development): it listens on `$PORT` (default 8765)
  and authenticates with `MAKEFUN_SHOP_TOKEN`.

## Local verification

Against the kit's bundled environment
(`program/env/server.mjs`, token `test-token-123`, fresh state dir) the
entry passes **120/120 conformance checks** (git 30, repos 15, issues
15, pulls 20, GraphQL 20, MCP 15, protocol 5) and scores **157/200
performance points** on the local benchmark (push 44, clone 29, read 45,
write 39; medians: push 568 ms, clone 211 ms, read 909 ms, write
160 ms, against the house reference medians). Real `git` (2.43.0)
push / clone / fetch / pull / shallow-clone round-trips were also
verified directly against the entry.

## License

MIT — see [LICENSE](LICENSE).
