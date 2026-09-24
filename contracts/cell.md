# Brainstem Agent cell contract (end-to-end v1)

Status: experimental, local single-owner host first. This contract supersedes
the "no real Grail / no credentials" limits of `m0.md` for the new cell code.
The offline M0 fixtures remain valid unit evidence and must keep passing.

## 1. Model: Grail is the captured mitochondrion

Brainstem Agent is the cell. Brainstem Grail is captured whole, like a
mitochondrion. It keeps its own genome (pinned, byte-identical source), its own
power source (the brainstem's existing GitHub Copilot connection) and its own
machinery (soul, three-round tool loop, streaming). The cell never edits,
patches, grafts or re-implements Grail. The cell supplies everything a more
capable organism needs around it.

| Cell part | Component | Rule |
| --- | --- | --- |
| Mitochondrial DNA | pinned Grail source + `data/grail-inventory.json` | Verify every tracked file before start and after stop. Mismatch discards the worker. |
| Mitochondrion | one private Grail worker process | One active run per worker. Fixed model per worker. |
| Inner membrane | Seatbelt sandbox, private tree/HOME/TMPDIR, scrubbed env | Only the cell talks to it. No owner secrets except the provisioned Copilot credential. |
| Protein import channel | `bridge/rapp_bridge_agent.py`, alone in a bridge-only `AGENTS_PATH` | The only foreign code inside. Forwards tool calls out. Holds no authority. |
| Nucleus | `state.py` Store, `policy.py` GrantAuthority | All durable state and authority. Never inside workers. |
| Cytoplasm | `broker.py` | Authenticates workers, resolves grants, routes calls to organs, writes receipts. |
| Organelles | `organs/*` | Bounded capabilities: files, memory, shell, web; later schedule, delegate, skills, channels. |
| Cell membrane | `host.py`, CLI, owner-only data directory | Owner authority enters here. |
| Fission / mitophagy | worker pool | Spawn fresh generations from verified DNA; destroy crashed, cancelled or damaged workers. |

Decisions:

- Inference uses only the brainstem's existing Copilot connection; other or
  multiple providers are out of scope.
- The runtime is qualified on macOS with Seatbelt (`/usr/bin/sandbox-exec`).
  Linux or VM hosting is a later deployment target. Evidence names its environment.
- Grail source reference: `kody-w/rapp-installer@49db80c8c6b6caa7647369beaf477d374a8f293c`,
  `rapp_brainstem/`, version `0.6.16`, kernel `brainstem.py` SHA-256
  `bd55a7f0bcf5efd3f7966ca39bb146da3c25fda9a0b1ce5ba587919d3c3775f4`.
  Never vendor Grail source into this repository; ship only hashes.

## 2. Package layout

All code is Python 3.11+, standard library only (the bridge may also import
Flask because it runs inside Grail's own interpreter). Tests use `unittest`.

| Path (under `runtime/brainstem_agent/`) | Role |
| --- | --- |
| `grail.py`, `data/grail-inventory.json`, `data/grail-requirements.lock` | pinned Grail source (hashes only), byte-for-byte verification, hash-locked worker venv |
| `credentials.py` | read-only discovery of the brainstem's existing Copilot connection |
| `worker.py`, `sandbox.py`, `paths.py` | one private, sandboxed Grail worker per generation; Seatbelt profiles; canonical paths |
| `bridge/rapp_bridge_agent.py`, `bridge/rapp_probe_agent.py`, `broker.py` | the only cell code inside a worker (and the `doctor --deep` probe); the loopback broker |
| `state.py`, `policy.py` | the single SQLite store; grant authority |
| `host.py`, `longturn.py`, `lifeline.py` | the membrane (`AgentHost`), long turns, process-group supervision |
| `daemon.py`, `schedules.py` | the always-on daemon and durable schedules |
| `knowledge.py`, `retrieval.py`, `session_index.py` | learned context, ranking and session search |
| `organs/*` | tools (section 7) |
| `health.py`, `credential_state.py` | the health model (liveness versus readiness, reasons and fixes); the credential's recorded state |
| `hygiene.py`, `observe.py` | retention, disk floor, bounded logs, compaction; the event log, log reading and store-derived statistics |
| `backup.py`, `release.py`, `lifecycle.py` | backup, restore and export; the release manifest, versions and zipapp; upgrade, rollback and uninstall |
| `cli.py`, `__main__.py` | the `brainstem-agent` command; `python -m brainstem_agent fixture` |
| `adapter.py`, `harness.py` | RAPP/1 envelope normalization; the offline M0 fixture harness |

Tests live in `runtime/tests/`. Tests that launch the real Grail process live
in `runtime/tests/test_real_*.py` and skip unless `BRAINSTEM_AGENT_REAL_CORE=1`.
Tests that spend real Copilot inference skip unless `BRAINSTEM_AGENT_LIVE=1`.
Both also skip, with the reason, when no verified Grail seed is available
(`BRAINSTEM_AGENT_GRAIL_SEED`, or the cache `setup` creates).

## 3. Data directory

`BRAINSTEM_AGENT_HOME` (default `~/.brainstem-agent`), owner-only `0700`:

```text
state/agent.sqlite3                       Store (0600; state/ is 0700)
state/search.sqlite3                      derived FTS5 session index (0600; rebuildable)
state/egress.jsonl, state/mcp-pins.json   outbound request log; pinned MCP tool definitions
state/pre-migration/*.sqlite3             copies taken before a store migration (0600)
state/credential.json                     a rejected credential's state (never the credential)
reach.json                                the owner's web egress policy and MCP servers
operations.json                           the owner's retention, log and disk policy (optional)
logs/events.jsonl                         the event log (0600, rotated by size)
backups/<UTC time>/                       backups (manifest.json with every file's SHA-256)
versions/<version id>/, versions/active.json   side-by-side runtime versions; the active one
cache/grail/<commit>/rapp_brainstem/      verified source cache (read-only; BRAINSTEM_AGENT_CACHE)
cache/venvs/<lock-sha256[:16]>-py311/     Grail worker interpreter (read-only to workers)
workers/<worker_id>/<generation>/         private worker tree, deleted after stop
  rapp_brainstem/                         byte-identical copy + untracked runtime files
  agents/                                 bridge-only AGENTS_PATH (dir 0500, file 0400)
  home/  tmp/                             private HOME and TMPDIR (0700)
  sandbox.sb                              rendered profile
logs/workers/<worker_id>-<generation>.log worker stdout/stderr (0600)
run/shell/<call_id>/                      private HOME/TMPDIR for shell commands
run/hosts/<host_id>/                      lifeline: flocked lease + one JSON record per live group
run/daemon.json                           running daemon: pid, port, bearer token (0600)
logs/daemon.log                           detached daemon output (0600)
workspaces/default/                       default workspace root
```

Tests always use a fresh temporary home from `tempfile.mkdtemp()` resolved with
`Path.resolve()`, never the real `~/.brainstem-agent`. Nothing may modify
`~/.brainstem`, port 7071, or any listener it did not start.

## 4. Sandbox (`sandbox.py`)

```python
class SandboxUnavailable(RuntimeError): ...

@dataclass(frozen=True)
class SandboxPolicy:
    read_denied: tuple[Path, ...] = ()   # subtrees whose file contents are unreadable
    readable: tuple[Path, ...] = ()      # re-allowed subtrees inside read_denied
    writable: tuple[Path, ...] = ()      # the only writable subtrees (plus /dev/null, ttys)
    write_denied: tuple[Path, ...] = ()  # denied even inside writable (files or subtrees)
    network: str = "none"                # "none" | "loopback" | "outbound"

def available() -> bool
def render(policy: SandboxPolicy) -> str                          # Seatbelt profile text
def wrap(argv: Sequence[str], policy: SandboxPolicy) -> list[str] # raises SandboxUnavailable
```

Paths must be absolute and are canonicalized with `realpath`. Among rules for
the same operation later rules win: `read_denied` < `readable`; `writable` <
`write_denied`. A rule for a specific operation beats a wildcard regardless of
order (measured on macOS 27), so every deny and its re-allow name the same
operation (`file-read-data`, `file-write*`, one network operation). Every
profile also denies Mach service lookups except
`com.apple.system.opendirectoryd.libinfo` (user and group names) and allows
signals only to processes in the same sandbox. There is no silent
fallback: callers that require a sandbox must refuse when it is unavailable.

## 5. Grail worker (`grail.py`, `credentials.py`, `worker.py`)

```python
# grail.py
PINNED_COMMIT: str; KERNEL_SHA256: str; VERSION: str
class GrailSourceError(RuntimeError): ...
@dataclass(frozen=True)
class GrailSource:
    commit: str
    root: Path                    # verified rapp_brainstem directory
    inventory: Mapping[str, str]  # relative POSIX path -> sha256, exact tracked set
def ensure_grail_source(home: Path, *, seed_dir: Path | None = None,
                        fetch: bool = True) -> GrailSource
def verify_tree(root: Path, inventory: Mapping[str, str],
                *, allow_extra: bool) -> list[str]   # returns untracked paths
def ensure_worker_venv(home: Path, *, python: str | None = None) -> Path  # venv python

# credentials.py
class CredentialUnavailable(RuntimeError): ...
@dataclass(frozen=True)
class GitHubCredential:
    value: str = field(repr=False)
    source: str   # "brainstem" | "env" | "file"
    kind: str     # "ghu" | "gho" | "ghp" | "github_pat" | "other"
def resolve_github_credential(source: str = "auto", *, environ=os.environ,
                              brainstem_home: Path | None = None) -> GitHubCredential

# worker.py
class WorkerError(RuntimeError): ...
@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str                # [a-z0-9][a-z0-9-]{0,31}
    home: Path
    source: GrailSource
    python: Path
    bridge_file: Path
    broker_url: str               # http://127.0.0.1:<port>
    credential: GitHubCredential | None   # None only for credential-free real-core tests
    model: str = "auto"
    sandbox: bool = True
    startup_timeout: float = 90.0
class GrailWorker:
    def __init__(self, config: WorkerConfig, *, register: Callable[[str, str], str]): ...
    worker_id: str; generation: str | None; url: str | None; log_path: Path | None
    def start(self) -> dict                  # startup evidence
    def health(self) -> dict
    def stream_chat(self, request: Mapping, grant: str, *,
                    read_timeout: float = 300.0) -> Iterator[bytes]
    def chat(self, request: Mapping, grant: str, *,
             timeout: float = 300.0) -> tuple[int, dict]
    def alive(self) -> bool
    def stop(self, *, grace: float = 2.0, timeout: float = 5.0) -> dict  # stop evidence
    def verify_integrity(self) -> list[str]
```

- `ensure_grail_source` verifies an existing cache, else copies a verified
  `seed_dir`, else downloads `https://codeload.github.com/kody-w/rapp-installer/tar.gz/<commit>`
  and safely extracts only regular files under `rapp_brainstem/` (no links,
  absolute paths, `..`, devices, duplicates). The exact tracked set and every
  SHA-256 must match `data/grail-inventory.json`. Cached files are read-only.
- `ensure_worker_venv` builds once from `data/grail-requirements.lock`
  (exact versions and hashes for Grail's `requirements.txt`) with
  `pip install --require-hashes --no-deps --only-binary=:all:`, writes a
  completion marker, verifies imports, and refuses Python < 3.11. Workers never
  install packages at runtime.
- Credentials: `auto` tries `BRAINSTEM_AGENT_GITHUB_TOKEN_FILE`, then the
  installed brainstem's `.copilot_token` (`$BRAINSTEM_HOME` or `~/.brainstem`,
  then `src/rapp_brainstem/.copilot_token`), then `GITHUB_TOKEN`. Token files
  must be regular, owner-owned, not group/world-readable and at most 4 KiB;
  JSON `{"access_token": ...}` or plain text. Read-only; never copy, log, print
  or persist the value. `repr()` never shows it.
- `start()` creates a fresh generation (`g` + 12 random hex), registers it with
  the broker to get a worker key, copies the verified source into the worker
  tree, verifies it, writes an empty `.env` there (stops python-dotenv's upward
  search), installs only the bridge file into `agents/`, renders the sandbox
  profile, and launches `python brainstem.py` in its own process group with
  exactly this environment:

  ```text
  HOME=<gen>/home  TMPDIR=<gen>/tmp  PATH=/usr/bin:/bin  LANG=en_US.UTF-8
  PORT=<free loopback port>  AGENTS_PATH=<gen>/agents
  SOUL_PATH=<gen>/rapp_brainstem/soul.md  GITHUB_MODEL=<model>
  GITHUB_TOKEN=<credential or empty>  BRAINSTEM_LAN_MODE=false  VOICE_MODE=false
  PYTHONDONTWRITEBYTECODE=1  PYTHONNOUSERSITE=1  PYTHONUNBUFFERED=1
  BRAINSTEM_AGENT_BROKER_URL=<broker_url>  BRAINSTEM_AGENT_WORKER_ID=<id>
  BRAINSTEM_AGENT_WORKER_GENERATION=<generation>  BRAINSTEM_AGENT_WORKER_KEY=<key>
  ```

- Worker sandbox: read-deny the owner's home except the worker generation, the
  venv and the Grail cache; writable only `<gen>/rapp_brainstem`, `<gen>/home`,
  `<gen>/tmp`; write-deny every tracked Grail file, the original `agents/`
  subtree, the bridge `agents/` directory and the venv; network `outbound`.
- Ready means `/health` returned HTTP 200 JSON. `status: unauthenticated` is a
  `WorkerError` unless `credential is None`.
- `stream_chat` POSTs `/chat/stream` on `127.0.0.1` with header
  `X-Brainstem-Agent-Grant: <grant>` and yields raw body bytes. Non-200 raises
  `WorkerError` carrying the parsed JSON error when present.
- `stop()` sends SIGTERM to the process group, SIGKILL after `grace`, confirms
  the group is gone within `timeout`, verifies tracked files, lists untracked
  runtime files, moves the log and deletes the generation tree. Evidence records
  exit status, seconds, integrity and untracked paths, never secrets.

## 6. Bridge and broker wire

The host sends each run to a worker with `X-Brainstem-Agent-Grant: <handle>`.
Grail re-imports every `*_agent.py` for each `/chat` and `/chat/stream`
request inside Flask's request context, instantiates the classes, collects
`system_context()` before streaming, and later calls `perform(**arguments)` on
those same instances. The bridge relies only on that existing behavior.

Bridge (`bridge/rapp_bridge_agent.py`, copied as a data file, never imported by
the package): stdlib + Flask only. At module execution inside a request that
carries the grant header it POSTs `/v1/bind`; on success it defines one public
`BasicAgent` subclass per returned tool (module-level attributes whose
`__module__` is the executing module), and the first class returns the bind
`context` from `system_context()`. Without a request context or header it
defines nothing. If binding fails it defines a single `brainstem_agent_status`
tool whose context and result explain that tools are unavailable. `perform`
POSTs `/v1/invoke` and returns the content string; it never raises. It uses a
proxy-free opener to `BRAINSTEM_AGENT_BROKER_URL` only.

Every broker request carries:

```text
X-Brainstem-Agent-Worker: <worker_id>
X-Brainstem-Agent-Generation: <generation>
Authorization: Bearer <worker_key>
Content-Type: application/json
```

```text
POST /v1/bind   {"grant": str, "session_id": str|null, "user_input": str}
  200 {"bind_id": str, "tools": [ToolSpec.to_wire()...], "context": str}
POST /v1/invoke {"grant": str, "bind_id": str, "call_id": str,
                 "tool": str, "arguments": object}
  200 {"ok": bool, "content": str}
errors: 400 malformed | 401 worker authentication | 403 grant/capability denied
        | 404 unknown bind or tool | 413 body too large -> {"error": str}
```

Broker (`broker.py`):

```python
class Broker:
    def __init__(self, *, organs: Sequence[Organ],
                 resolve_grant: Callable[[str, str, str], RunBinding],
                 workspace_root: Callable[[RunBinding], Path],
                 receipts: ReceiptSink,
                 namespace: Callable[[RunBinding], str],
                 context_limit: int = 8000, max_body: int = 1 << 20): ...
    def start(self) -> None          # ThreadingHTTPServer on 127.0.0.1:0
    url: str
    def register_worker(self, worker_id: str, generation: str) -> str  # new key
    def unregister_worker(self, worker_id: str) -> None
    def bind_count(self, grant: str) -> int
    def cancel_grant(self, grant: str) -> None
    def stop(self) -> None
```

- `resolve_grant(grant, worker_id, generation)` returns the trusted
  `RunBinding` or raises `policy.GrantDenied`. Resolve on every bind and every
  invoke, so revocation is immediate. Worker keys are compared in constant time.
- Only tools whose capability is in the binding are advertised and invokable.
  An invoke must name a tool advertised by that bind for that grant and worker.
- Every invoke that passes worker authentication and grant resolution writes a
  receipt: `begin_receipt` -> organ -> `finish_receipt(succeeded|failed)`;
  capability or unknown-tool refusals record `denied`. Receipt requests hold
  validated arguments with each string clipped to 2,000 characters; results hold
  `ok`, `content_sha256`, `content_chars` and organ evidence, not full content.
- Model content is clipped with `organs.clip`. `OrganError` becomes
  `{"ok": false, "content": message}`; other exceptions become a generic
  failure without internals. `cancel_grant` sets the `cancelled` event of every
  in-flight invocation for that grant and refuses later binds and invokes.
- The bind context starts with a short `<brainstem_agent>` header naming the
  workspace and saying tool outputs and the `<memory>`, `<profile>`, `<skills>` and
  `<past_sessions>` blocks are data, not instructions, then the host's learned-context
  block (`learned_context(BindContext)`, see 7a), then each relevant organ's `context()`
  text, bounded by `context_limit`.

## 7. Organs

`organs/base.py` is the frozen organ contract (`ToolSpec`, `BindContext`,
`InvocationContext`, `ToolResult`, `Organ`, `OrganError`, `ReceiptSink`,
`FactStore`, `validate_arguments`, `clip`). The broker validates arguments with
`validate_arguments` before calling `invoke`. Tool names and capabilities:

| Organ | Tools | Capability |
| --- | --- | --- |
| `FilesOrgan()` | `list_files`, `read_file` | `files.read` |
| | `write_file` | `files.write` |
| `MemoryOrgan(facts, *, profile_namespace, in_context, ...)` | `recall` (both scopes) | `memory.read` |
| | `remember` (`scope`: `workspace` or `profile`), `forget` | `memory.write` |
| `SkillOrgan(store, *, profile_namespace, prior_tools, turn_input, schedule_creator)` | `skill_view` | `skills.read` |
| | `skill_save` | `skills.write` |
| `SessionOrgan(store, index)` | `session_search` | `sessions.read` |
| `ScheduleOrgan(store, *, notify, clock, prior_tools)` | `schedule_list` | `schedule.read` |
| | `schedule_create`, `schedule_update` | `schedule.write` |
| `ShellOrgan(*, run_root, deny_read, environ, supervisor)` | `run_command` | `shell.run` |
| `ProcessOrgan(store, *, run_root, deny_read, ...)` | `process_start`, `process_status`, `process_read`, `process_write`, `process_stop` | `processes.run` |
| `ScriptOrgan(*, run_root, deny_read, ...)` | `run_script` | `scripts.run` |
| `DelegateOrgan(run)` | `delegate_tasks` | `agents.delegate` |
| `WebOrgan(*, config, log, environ)` | `web_fetch` | `web.fetch` |
| | `web_search` | `web.search` |
| `McpOrgan(*, config, run_root, deny_read, log, pins, ...)` | `mcp__<server>__<tool>` for each allowed tool of a configured server | `mcp.<server>` |

Rules: every path is relative to `InvocationContext.workspace_root`; never
follow symlinks or leave the root; refuse hard-linked files (link count above
1) and non-regular files such as FIFOs without blocking, and never list a
hard-linked file's metadata. Memory facts are scoped to the run
namespace (workspace) or the owner's profile namespace. The web organ refuses
non-public addresses (loopback, private, link-local, multicast, reserved,
including after redirects and DNS resolution). Shell commands run inside
`sandbox.wrap` with network `none`, writes limited to the workspace and a
private per-call directory, the owner's home unreadable except the
workspace, desktop services (LaunchServices, preferences, keychain, pasteboard,
Spotlight) unreachable, and signals limited to the command's own sandbox;
unavailable sandbox refuses. Every result is honest about failures,
truncation and uncertainty. Every advertised tool declares at least one
required argument: unchanged Grail refuses a streamed call whose argument
string is not a JSON object, and models stream no arguments for a tool that
requires none. `validate_arguments` fills a missing property's declared
`default` (checked against its schema when the tool is declared) and treats
`null` for an optional property as not given.

## 7a. Learned context (`retrieval.py`, `knowledge.py`, `session_index.py`)

- One ranking core: BM25 (Lucene IDF), a match gate (query-term coverage and a share of
  the best score), recency and usage signals; `DEFAULT_PARAMS` tuned offline on
  `runtime/tests/retrieval_eval.py`.
- `knowledge.gather(store, namespace, profile, workspace_root, query, capabilities, now)`
  reads the candidates fresh on every bind: `AGENTS.md`/`BRAINSTEM.md` (always),
  profile and workspace facts (memory capability), offered skills (`skills.read`: active,
  not quarantined; workspace scope first, then the profile scope), the session pointer
  (`sessions.read`). `knowledge.assemble` renders them within `BUDGET` = 6,000 characters
  (shares and maxima per section, leftovers in the order instructions, profile, memory,
  skills; the session pointer and joining newlines reserved first, sections giving way in
  reverse order when the rest is below the shares' sum, no section above its allowance),
  dropping whole items with explicit "+N more not shown" notes and shortening an
  instruction file by relevance with a note naming the omitted sections. The host records
  the report in the turn's evidence (`context`: sizes and ids, no text).
- Skills are data: `skill_save` by the model is `unreviewed`; a save after an earlier
  tool in the same turn returned text the cell did not write, unless the owner's own words
  explicitly asked to save that skill (`organs.skills.owner_request`: a saving verb whose
  object is a skill, outside quotes, not negated; named skills only, or one skill when none
  is named), is quarantined (new skill) or pending (new version). Only the owner approves
  (a pending version only by its number; a stale one never), rejects, disables, edits,
  shares or deletes. No skill text changes a grant. Facts and skills refuse
  credential-shaped text (`credentials.credential_kinds`); receipts record it redacted.
- A scheduled run's prompt is the owner's request only while `created_by` is `owner`; a
  conversation that rewrites a prompt becomes its `created_by`, and a turn that has read
  outside text cannot rewrite an owner-written prompt or name.
- `SessionIndex(path)` is a derived FTS5 index over succeeded turns (repaired from the
  store, rebuilt if broken, every hit re-read from the store); without FTS5 a BM25 scan of
  the newest 2,000 turns. Searches are scoped to one workspace namespace.

## 8. Nucleus (`state.py`, `policy.py`)

Store schema version 2 migrates version 1 in one transaction on open (never
resets). New public methods (existing ones unchanged):

```python
def get_chat(self, owner, turn_id) -> ChatReservation
def list_sessions(self, owner, *, limit=50) -> list[dict]
    # session_id, turns, last_input (<=120 chars), last_state
def begin_receipt(self, namespace, turn_id, call_id, tool, capability, request) -> str
def finish_receipt(self, receipt_id, state, result) -> None
    # started -> succeeded | failed | denied | uncertain, exactly once
def list_receipts(self, namespace, *, turn_id=None, limit=100) -> list[dict]
def add_fact(self, namespace, text, *, source_turn=None) -> dict
def update_fact(self, namespace, fact_id, text) -> dict
def delete_fact(self, namespace, fact_id) -> bool
def list_facts(self, namespace, *, limit=200) -> list[dict]      # newest first
def search_facts(self, namespace, query, *, limit=20) -> list[dict]
def append_run_event(self, owner, turn_id, event) -> int         # 1-based seq
def run_events(self, owner, turn_id, *, after=0, limit=1000) -> list[tuple[int, dict]]
# Durable scheduling (tables schedules + occurrences, added to v2 in place):
def create_schedule(self, namespace, *, workspace, created_by, name, spec, timezone,
                    prompt, capabilities, missed_policy, next_fire_at) -> dict
def get_schedule(self, namespace | None, schedule_id) -> dict
def list_schedules(self, namespace=None, *, include_removed=False) -> list[dict]
def update_schedule(self, namespace | None, schedule_id, **changes) -> dict
def next_wake(self) -> float | None           # min next_fire_at / run_now_at: the next-fire index
def claim_due(self, now, plan, *, schedule_id=None) -> dict | None
    # one transaction: the earliest due schedule, plan(schedule, last_run, now), insert the
    # occurrence occ_<schedule>_<instant>[_m] (never twice) and advance next_fire_at;
    # plan returns None to claim nothing (the loop's overlap-only claims during a run)
def finish_occurrence(self, occurrence_id, state, *, turn_id=None, result=None, now=None) -> dict
    # keeps the missed_count recorded at the claim (so does recover_interrupted)
def get_occurrence(self, occurrence_id) -> dict | None
def list_occurrences(self, namespace=None, *, schedule_id=None, states=None, limit=50) -> list[dict]
# The learning cell (tables skills, skill_versions, fact_uses, turn_log, added to v2 in
# place from the Cell v1 or scheduling layout):
def save_skill(self, scope, name, *, description, when_to_use, steps, author, review,
               tainted=False, note=None, session_id=None, turn_id=None, workspace=None,
               pending=False, allow_disabled=False) -> dict   # outcome created|updated|pending
def get_skill(self, scopes, name, *, version=None) -> dict | None
def list_skills(self, scopes, *, offered=False) -> list[dict]
def skill_history(self, skill_id) -> list[dict]                 # every version, oldest first
def set_skill(self, skill_id, *, state=None, scope=None) -> dict
def approve_skill(self, skill_id, *, version=None) -> dict      # current, or exactly `version`
    # (the current or the pending one; a pending version older than the current is refused)
def reject_pending_skill(self, skill_id) -> dict
def delete_skill(self, skill_id) -> bool                         # every version's text
def note_skill_use(self, skill_id) -> None
def note_fact_uses(self, fact_ids) -> None
def fact_uses(self, namespace) -> dict[str, int]
def list_turns(self, namespace, *, limit=2000, turn_ids=None) -> list[dict]  # succeeded, with times
def count_turns(self, namespace) -> int                           # both skip forgotten turns
def forget_session(self, namespace, session_id) -> dict
    # blanks the session's inputs, answers, its scheduled runs' inbox answers, its receipts'
    # payloads and run events; keeps rows, states and replay keys
```

`recover_interrupted()` additionally marks `started` receipts uncertain, and a
`running` occurrence `uncertain` when its turn (found through the occurrence id,
which is the turn's idempotency key) had started a tool, else `failed`. Fact dicts are `fact_id`, `text`, `created_at`,
`updated_at`, `source_turn`; text is at most 2,000 characters and a namespace
holds at most 5,000 facts. Deletion removes the text.

Policy: `GrantAuthority(store, *, clock=time.time, mode="synthetic"|"local")`.
`local` is single-owner authority for the owner's own host (not multi-user
authentication); TTL up to 3,600 seconds. Grants are stored under the SHA-256
of the handle, never the raw handle. New
`resolve(token, *, worker_id, generation) -> RunBinding` checks authority mode,
revocation, clock, expiry and worker generation. `RunBinding.storage_namespace`
is the single namespace function; `profile_namespace(owner)` names the owner's
cross-workspace memory scope.

## 9. Host and CLI

`AgentHost` owns the store, authority, broker, organs and worker pool. A turn:
reserve chat -> issue grant bound to worker generation -> mark running ->
`stream_chat` with history from the store -> journal events -> normalize the
final `done` -> require at least one bind for the grant -> finish -> revoke.
Missing bind, core error or EOF without `done` never becomes success. A crash
after any receipt started is `uncertain`. A receipt the store cannot begin means
the tool is not run and the turn fails; one it cannot finish makes the turn
`uncertain`. The broker drops a finished grant's bookkeeping once it is revoked. Cancellation revokes and cancels the
grant, stops the worker's process group and replaces it.

Lifecycle (`lifeline.py`): every group the host starts is recorded under
`run/hosts/<host_id>/`; a watchdog kills them when the host dies (even by
SIGKILL), and the next host start reaps records of hosts whose lease is free,
killing only a pid with the recorded start identity and program. A tracked
program is launched through a gate (`lifeline.gated_popen`) that execs it only
after its pid is recorded, so a host dying in between leaves nothing unrecorded.
Stop and cleanup treat macOS's EPERM for zombie-only groups as "zombies", reap
and then confirm the group is gone. A turn ends only after its cancelled tools stopped;
`close()` stops in-flight tools first. A `setsid()` descendant escapes group
kills but stays inside its sandbox.

CLI (`brainstem-agent`, or `python -m brainstem_agent`): `setup`, `doctor`,
`chat MESSAGE`, `tool NAME`, `memory`, `profile`, `skills`, `sessions [search QUERY]`,
`context MESSAGE`, `receipts`, `turns`, `processes`, `cancel`, `serve`, `status`, `stop`,
`schedules`, `inbox`, `service`, `mcp`, `egress`, and the operations commands `version`,
`backup`, `restore`, `export`, `upgrade`, `rollback`, `uninstall`, `prune`, `compact`, `logs`
and `stats`; every command accepts `--json`.
`python -m brainstem_agent fixture` runs the offline M0 harness.

## 9a. Always-on cell (`daemon.py`, `schedules.py`)

- `serve` (foreground, or `--detach` in its own session with `logs/daemon.log`)
  runs one `Daemon` per home. It holds `state/host.lock` for its lifetime, so a
  second `serve` (or an in-process turn) refuses; interrupted work is recovered
  once at start. It owns one `AgentHost` whose grant clock never regresses, one
  warm worker (re-verified against the inventory before reuse and while idle;
  replaced on mismatch) and one scheduler loop thread. Turns (chat and scheduled)
  run one at a time on that worker; per-turn workspaces are supported.
- Control surface: loopback HTTP, bearer token only in `run/daemon.json` (0600),
  Host header must be the loopback address. RAPP/1 `POST /chat`; private
  `GET /v1/status` (health, warm workers, scheduler, last errors), `POST /v1/turn`
  (full `TurnResult`, used by the CLI), `/v1/tool`, `/v1/cancel` (by request id),
  `/v1/wake`, `/v1/stop`. The CLI routes `chat` and `tool` through a running
  daemon, else runs them in-process; schedule changes are written to the store
  and wake the loop.
- `Scheduler` is the next-fire loop: `tick()` claims and runs everything due;
  `run()` then keeps the worker warm and sleeps until `next_wake()` (at most 30 s).
  A claimed turn runs on a helper thread while the loop thread watches that
  schedule and records each instant (or run-now request) that comes due meanwhile
  as `skipped` (`overlap`) when it arrives. `plan()` decides each occurrence:
  run-now requests first; instants due while the same schedule was running are
  `skipped` (`overlap`; one record with a count for a stretch the loop could not
  see in time); a missed instant (not the latest pending, older than the daemon's
  start or over 60 s late) follows `missed_policy` (`run-latest-once-late` or
  `skip`); a schedule that cannot be evaluated is paused with a `skipped` record,
  never hot-looped. Every occurrence records `missed_count`: the scheduled instants
  it accounts for that never ran (0 on time; the stretch minus the late run; the
  whole stretch when skipped).
- DST rule: each matching wall time fires once, at the first instant the clock
  reads it (first pass of a repeated time); a wall time skipped by spring-forward
  fires shifted forward by the gap (the old offset, PEP 495 `fold=0`).
- `status` reports the worker `state` (`starting` until its start completed,
  `warm`, `busy`, `stopped`) and survives a failing store (`store.ok`); `stop`
  measures every worker group the daemon had or stopped (the daemon writes
  `run/last-stop.json` before releasing the home). A failing store while the
  daemon starts (open, recovery) is retried with backoff for up to 60 s.
- `ScheduleOrgan` tools `schedule_create` / `schedule_list` / `schedule_update`
  (`schedule.write` / `schedule.read`). A turn may only create schedules with
  capabilities it holds itself (default: its own minus `schedule.*`) and may only
  change schedules whose capabilities it holds. `doctor --deep` still proves the
  bridge with exactly the core organs (`CORE_CAPABILITIES`).
- `service install|uninstall [--dry-run]` renders and loads a per-home LaunchAgent
  (`plistlib`, `launchctl bootstrap|bootout gui/<uid>`).

## 9b. Operable cell

- Health (`health.py`): every check has `id`, `kind` (`liveness` or `readiness`), `ok`,
  `required`, `reason` and `fix`. Live: the process answers and its schedule loop runs.
  Ready: Grail verified, worker interpreter prepared, credential usable, sandbox, store
  schema readable, disk above the floor, warm worker, MCP servers healthy, not draining.
  `doctor` (installation), `status --json` (`readiness`) and the daemon's `GET /v1/health`
  report it; `GET /v1/version` and `POST /v1/drain` (finish running work, start nothing new;
  `resume` undoes it) are the daemon's other operational routes.
- Credential state (`credential_state.py`): a rejection classified from Grail's own errors
  (`invalid` or `no_access`) is recorded with the credential file's identity (never its
  value); new turns are refused before anything is written or started and the daemon starts
  no worker until the file changes (then the new token is used without a restart) or a
  backoff (10 minutes doubling to 6 hours) allows one probe.
- Hygiene (`hygiene.py`): `operations.json` policy; new turns are refused below the disk
  floor; bounded logs; `prune` (and `--dry-run`) over receipts (never a running turn's or a
  schedule's provenance), run events, inbox entries, the egress log and pre-migration copies;
  `compact` (VACUUM, exclusive).
- Store safety (`state.py`): `inspect_database` and `classify_schema` read a store without
  writing (`current`, `migrates`, `newer`, `foreign`, `unknown`); an older layout is copied
  with the online backup API before its migration transaction; a newer one is refused
  without a write; `backup_database` makes integrity-checked online copies.
- Releases (`release.py`, `lifecycle.py`): `data/release-manifest.json` lists every shipped
  file's SHA-256 and the store schemas a version reads and migrates; upgrade and rollback
  switch between verified side-by-side versions in `versions/` (the CLI, daemon and
  LaunchAgent follow `versions/active.json`), draining the daemon first; a rollback the store
  no longer fits refuses unless given the pre-upgrade backup to restore. The zipapp unpacks
  itself into `versions/` after verifying every file.

## 10. Evidence classes

- `unit`: fakes and fixtures only.
- `real-core`: the unchanged Grail process, no inference.
- `live`: real Copilot inference through the brainstem's existing connection.

Evidence names the class and environment (for example `macos-seatbelt`). Never
print, log, persist or return credentials, worker keys or grant handles.
