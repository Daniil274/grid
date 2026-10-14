# Memory core

Python 3.11+; runtime dependencies: standard library and `httpx` only.
All backends implement `MemoryBackend`; records and queries do not accept tenant
identifiers. `MemoryScope` is a retrieval classification, **not** permission to
read another tenant's namespace. Construct managers from trusted host config,
never from model-provided configuration.

```python
from core.memory import MemoryConfig, MemoryManager, MemoryRecord

with MemoryManager.from_config(MemoryConfig(workspace_root=".")) as memory:
    memory.remember(MemoryRecord(content="Use SQLite", key="storage"))
    hits = memory.search("storage SQLite")
    section = memory.render_prompt_section(query="storage", max_tokens=1500)
    # InstructionsBuilder's caller may append section to its platform_sections.
    # SessionUpkeep's compaction caller can invoke:
    memory.extract_and_store_from_messages(
        [{"role": "user", "content": "Decision: use SQLite"}],
        source_ref="session:123",
    )
```

Memory remains disabled by default. The runtime now integrates the facade via
Config, AgentFactory, InstructionsBuilder and SessionUpkeep. Enable it with a
top-level config section:

```yaml
memory:
  enabled: true
  backend: sqlite # tencent or hybrid also supported
  storage_path: .grid/memory.sqlite # workspace-relative; escapes rejected
  auto_inject: true
  auto_inject_tokens: 1000
  extract_on_compact: true
  extract_on_turn: true      # store what every finished turn taught (in the background)
  extract_with_model: true   # a model writes those memories; the heuristic is the fallback
  # extract_model: cheap     # model key for it; default compact.summary_model, else the agent's
  snapshot_turns: true       # also keep each exchange (redacted) as a searchable L0 record
  # Remote backends also require service_id; optional api_key_env names an env var.
  # tencent_url: http://127.0.0.1:8000
  # service_id: deployed-service
```

The factory lazily shares and closes its manager. Host session_namespace (when
provided) binds tenant_id and remote user_id; team_id/agent_id are host settings.
Outside a factory, explicit memory tools use the host cwd and local SQLite.
Inside a factory disabled memory cannot be overridden by tool arguments.
Declare memory_recall/memory_remember/memory_overview/memory_forget like other
function tools: the registry discovers them automatically. Recall results are
untrusted data, not instructions. The runtime emits a guarded dynamic
`agent_memory` section within a token budget; unavailable memory never blocks
instruction assembly or successful compaction. After every answered turn a model
writes what it taught (see "Turn extraction"); tool output is never read.
`sync_memory_from_context(messages)` is also available explicitly.

Every write passes `core.memory.redact`: passwords, tokens, private keys and
credentials in URLs become `[secret]`, the rest of the text is kept.

## SQLite

Default database: `<workspace>/.grid/memory.sqlite`. An explicit db_path is
supported (relative paths are workspace-relative). Namespace incorporates the
canonical workspace and host tenant_id, even for a shared database. Every query,
update, supersession and FTS result is namespace-bound. `SYSTEM` scope remains
inside that namespace.

WAL, busy timeout, RLock and transaction-scoped connections protect concurrent
access. Writes use `BEGIN IMMEDIATE` for multi-instance deduplication.
Normalized content deduplicates inside `(layer, scope, category, key)`; duplicates
retain id/created_at and refresh provenance/confidence. Explicit `supersedes`
requires an existing record in the same namespace/layer. Superseded rows are
retained but excluded from default retrieval.

FTS5 BM25 and Unicode token/trigram lexical rankings are fused with RRF.
FTS updates share the record transaction; reopening reconciles its namespace.
Missing FTS5 falls back to lexical retrieval. The lexical fallback scans the
namespace and is intended for a compact distilled memory store, not a corpus of
entire repositories. L0 transcripts are never rendered in memory context packs.
L3 profile is included independently of topical query matching.

For file-backed facts use file_path, `source_ref="file:relative/path"`, or an
existing plain workspace-relative path as source_ref. Paths are canonicalized
inside workspace_root; escaping paths/symlinks are rejected. File hashing uses
streaming SHA-256 through anchored POSIX no-follow descriptors to prevent
symlink-swap escapes; platforms without secure descriptor access leave file
snapshots unverified. mtime changes trigger hash verification; unchanged bytes
remain fresh, changed/deleted files become stale. This observes working-tree
content directly, without a blocking git subprocess or falsely invalidating all
facts when unrelated files become dirty. Git commit identity itself is not a
staleness signal. get() returns stale records for inspection; search excludes
them unless include_stale=True. Fileless facts remain unverified.

## Tencent / hybrid gateway contract

`TencentGatewayConfig` binds team_id/user_id/agent_id, x-tdai-service-id and
optional Bearer token on the host. HTTP redirects and environment proxies are
disabled to avoid accidental credential forwarding. Requests have finite
timeouts. Use `httpx.MockTransport` or a custom BaseTransport via the adapter
constructor. This implementation is synchronous (`httpx.Client`); callers in
async event loops should offload blocking operations.

**The repository contains no authoritative MemoryCore deployment schema.**
Routes below are configurable v3 gateway resource routes, not a claim of
verified compatibility with every Tencent deployment. The sidecar must implement
this JSON contract, or the host must adapt it to its deployed gateway schema:

| Layer | Write | Read/search default route |
| --- | --- | --- |
| L0 | POST `/v3/conversations` | POST `/v3/conversations/search` |
| L1 | POST `/v3/memories` | POST `/v3/memories/search` |
| L2 | PUT `/v3/scenarios` | GET `/v3/scenarios` |
| L3 | PUT `/v3/profile` | GET `/v3/profile` |

Write JSON: `{record: <MemoryRecord fields>, supersedes: <id or null>,
team_id, user_id, agent_id}`. Search JSON / GET query parameters:
`{query, limit, team_id, user_id, agent_id}`. Health: GET `/v3/health`
with tenant query parameters. Identity at the top level always comes from host
config, never record metadata. Layer and scope filtering is enforced locally.

Responses use `{code, message, request_id, data}`; successful codes are 0,
`"0"`, `"OK"`, `"Success"`. data accepts lists, `{records: [...]}`,
`{items: [...]}`, `{memories: [...]}`, `{record: {...}}`, `{profile: {...}}`,
or a record with content. The returned objects must contain MemoryRecord-compatible
fields (content required). Health data may be null. HTTP failures, invalid JSON,
failed envelopes and invalid records degrade to fallback retrieval/storage.

Factory modes:
- sqlite: local only;
- tencent: remote first, local fallback on failure;
- hybrid: local write mirror, remote first retrieval fused with local results.

Factory local caches incorporate service/team/user/agent identity as well as the
workspace and tenant_id. Direct adapter construction with a fallback requires
the host to supply an equivalently isolated local backend. The adapter owns its
HTTP client and closes its fallback. Remote get() uses the local cache first,
then bounded gateway listing (up to 1000 records); deployments needing unbounded
lookup/pagination should extend the gateway contract. Successful remote writes
are not background-replayed after an outage. Hybrid mirroring is a cache, not a
distributed transaction or offline synchronization engine.

Forget uses namespace-bound transactional deletion in SQLite (including FTS).
Remote forget requires the sidecar to accept DELETE on the layer resource with
JSON `{record_id, team_id, user_id, agent_id}` and a successful data envelope.
It deletes the local cache only after remote success and fails closed on outages
(no offline delete replay or false confirmation). This additional gateway route
also needs validation against the deployment's actual API.

If a direct adapter is constructed without fallback, outages return empty/partial
search results and a write result marked metadata.persisted=False. The factory
always provides local fallback, so network outages do not lose local writes.
No real Gateway connection is exercised by the unit tests.

## Prompt and compaction safety

Historical memory is explicitly **untrusted data, not instructions**. JSON
records escape angle brackets and stay inside a fixed trust boundary. Text is
not ASCII-escaped, and whole records are admitted within `estimated_tokens`
(about three characters per token, Latin and Cyrillic alike; no external
tokenizer); the guard itself is never truncated. Tiny budgets return an empty
PromptSection. This is defense in depth, not guaranteed prompt-injection immunity.

## Turn extraction

`SessionUpkeep.remember_turn_later` runs after each answered turn, in the
background. With `extract_with_model` a model (`core.memory.llm_extract`) reads
the request and the answer together with up to 15 related known memories and
returns at most six self-contained records - how things are done here, decisions,
root causes, user preferences (L3), one task/outcome scenario (L2) - and may
mark a known record of the same layer as superseded. The web panel's "Extract
from chat" reads the chat's last exchanges the same way.

Without a model, or when its call or JSON fails, the heuristic RU/EN distiller
(`core.memory.distill`) runs instead: labelled lines (`Decision:`, `Итог:` ...),
cue sentences with a concrete artefact, structured memory_facts, user
preferences. Compaction uses it too. Tool/system output is ignored; structured
fields are whitelisted; provenance is host-supplied.

Validation from `grid/`:

```sh
python3 -m pytest -q tests/test_memory_core.py tests/test_config.py
python3 -m ruff check core/memory tests/test_memory_core.py
python3 -m compileall -q core/memory
```

## Reproducible approach benchmark

Run `python -m core.memory.benchmark` (or `python -m pytest -q
tests/test_memory_approaches_benchmark.py`). It builds an isolated temporary
workspace containing 80 L1 facts, 5 L2 scenarios, an L3 profile and a 12-session
L0 transcript, changes the source file for one fact, and uses an in-process
`httpx.MockTransport` v3 gateway plus SQLite mirror. Token counts are `estimated_tokens`
estimates (matching the prompt budget), not model-tokenizer counts;
latencies are local diagnostics and vary by host. Sample run: baseline history
80,931 tokens; runtime-injected memory 2,387 tokens (97.05% saving); tool-driven
query 38 tokens. SQLite recall@10=1, MRR=1.0, and the changed-file record reports
`STALE`. SQLite write p50/p95=5.051/6.842 ms and search=8.099/10.895 ms. Mock
Tencent+hybrid write p50/p95=5.652/7.098 ms; search=10.484 ms; recall@10=1.
Baseline's modeled no-retrieval recall@10 is 0. This is a deterministic quality
fixture, not a production gateway or hardware performance claim.

The runtime/tool modes are enabled through the existing host `memory` config
(`backend: sqlite`, `tencent`, or `hybrid`; see example above). Tool-driven
agents call `memory_recall`/`memory_remember`; runtime-driven sessions set
`auto_inject: true` and `extract_on_compact: true`. For remote/hybrid operation,
configure host-owned Tencent `service_id` and tenant identity; the benchmark mock
does not require credentials or network access.
