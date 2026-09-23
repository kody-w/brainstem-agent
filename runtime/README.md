# Brainstem Agent runtime (experimental, 0.1.0)

Brainstem Agent is a cell that captures **Brainstem Grail** unchanged as its
mitochondrion: pinned, byte-verified Grail source (hashes only are shipped in
`brainstem_agent/data/`), powered by the installed brainstem's existing GitHub
Copilot connection. The cell supplies the membrane (Seatbelt sandbox, private
worker trees), nucleus (SQLite state, grants, receipts, memory), cytoplasm
(loopback broker) and organelles (files, memory, shell, scripts, background
processes, skills, schedules, helpers, web and MCP). Grail is extended only
through its supported seams: one bridge agent file in a bridge-only
`AGENTS_PATH`, `SOUL_PATH`, environment variables and HTTP. macOS only;
qualified on Apple silicon (arm64) with `/usr/bin/sandbox-exec`, Python 3.11 and 3.13.

## Install

From a clone of this repository (the root [README](../README.md) has the whole install,
including the Brainstem core and its GitHub sign-in):

```sh
python3.11 -m venv .venv
.venv/bin/pip install ./runtime
.venv/bin/brainstem-agent setup
.venv/bin/brainstem-agent doctor --deep
```

`pip install ./runtime` installs the `brainstem-agent` command (standard library only);
`setup` downloads and verifies the pinned Grail and builds its venv; `doctor --deep` proves
the sandbox, worker and sign-in are ready. The installed command `brainstem-agent ...` and the source form
`PYTHONPATH=runtime python3.11 -m brainstem_agent ...` (from the repository root, no
install) take the same arguments (only the offline `fixture` command needs the source
form); the examples below use the source form. `pip install ./runtime` fetches its build
backend (setuptools) from PyPI; offline, a Python 3.11 venv (which includes setuptools) can
use `.venv/bin/pip install --no-build-isolation ./runtime`.

## Quick start (headless; every command accepts `--json`)

```sh
export PYTHONPATH=runtime
python3.11 -m brainstem_agent setup
python3.11 -m brainstem_agent doctor
python3.11 -m brainstem_agent doctor --deep
python3.11 -m brainstem_agent chat "Create notes/hello.txt containing hi" --json
python3.11 -m brainstem_agent chat "What did I say?" --session <session_id>
python3.11 -m brainstem_agent chat "..." --idempotency-key k1
python3.11 -m brainstem_agent tool read_file --arguments '{"path": "notes/hello.txt"}'
python3.11 -m brainstem_agent memory --json
python3.11 -m brainstem_agent sessions --json
python3.11 -m brainstem_agent receipts --json
```

`setup` verifies (or fetches) the pinned Grail and builds the hash-locked venv. `doctor`
reports readiness (source, interpreter, credential, sandbox); `doctor --deep` also starts a
probe worker to prove the bridge and membrane. Repeating a chat with the same
`--idempotency-key` replays it without a new Grail call. Words in angle brackets, such as
`<session_id>`, are placeholders for values from earlier output.

`chat` exits 0 (succeeded), 1 (failed), 3 (uncertain: a tool had started),
4 (cancelled by SIGINT/SIGTERM, which revokes the grant and kills the worker's
process group) or 5 (partial: a limit ended a long turn; see below). `tool` exits 0 (ok),
1 (failed) or 4 (cancelled by SIGINT/SIGTERM; the command's process group is killed).
`--capabilities` narrows the grant (for example `files.read`).

## Always-on cell (daemon and durable schedules)

```sh
python3.11 -m brainstem_agent serve --detach
python3.11 -m brainstem_agent status --json
python3.11 -m brainstem_agent chat "In 2 minutes, write the current time into notes/time.txt"
python3.11 -m brainstem_agent schedules list --json
python3.11 -m brainstem_agent schedules create --prompt "Summarize notes/" --cron "0 9 * * 1-5" \
    --timezone America/New_York --missed skip --capabilities files.read,files.write
python3.11 -m brainstem_agent inbox --json
python3.11 -m brainstem_agent stop --json
python3.11 -m brainstem_agent service install --dry-run --json
python3.11 -m brainstem_agent service uninstall --dry-run --json
```

`serve` runs one daemon per home (a second refuses; without `--detach` it stays in the
foreground). `status` reports health, warm workers, schedules and last errors. `schedules`
also takes `show`, `edit`, `pause`, `resume`, `run-now`, `remove` and `runs` with a schedule
id. `inbox` lists the results of scheduled runs, newest first. `stop` stops cleanly and
confirms the worker groups are gone. `service install` and `service uninstall` manage the
launchd LaunchAgent; with `--dry-run` they only print what they would do.

- **Daemon.** `serve` holds the home's exclusive lock for its lifetime (recovering
  interrupted work once at start), keeps one warm Grail worker and runs one loop.
  Chat and scheduled turns run one at a time on that worker. A warm worker is
  reused only after its copy re-verifies against the pinned inventory (also
  re-checked while idle); a mismatch replaces it. While a daemon runs, `chat` and
  `tool` go through it; without one they run in-process exactly as before. A
  store that is briefly locked or failing while the daemon starts (opening it,
  recovery) is retried with backoff for up to 60 s and shown in `last_errors`;
  a failing store never takes `status` down (`store.ok` false, health
  `degraded`).
- **Status and stop.** `status` gives the worker's `state`: `starting` until its
  start has completed (never counted as warm), then `warm` (idle), `busy` (a
  turn is using it) or `stopped`. `stop` reports every worker group the daemon
  had or stopped, each measured after the stop (`workers[].group_state`); the
  daemon also writes this to `run/last-stop.json` before it releases the home,
  so a worker a cancelled turn had already stopped is reported too.
- **Control surface.** HTTP on 127.0.0.1 with a random bearer token that exists only
  in `run/daemon.json` (0600; `run/` 0700). Requests without it, or with a Host that
  is not the loopback address, are refused (401/403). Workers and shell commands
  cannot reach it: their Seatbelt profiles deny loopback (except a worker's own
  broker) and reading the home. Routes: RAPP/1 `POST /chat` (exactly `response`,
  `agent_logs`, `session_id`), and `GET /v1/status`, `POST /v1/turn`, `/v1/tool`,
  `/v1/cancel`, `/v1/wake`, `/v1/stop`. The token is never printed or logged.
- **Next-fire loop.** Schedules live in the store with a `next_fire_at` column and
  index. The loop sleeps until the earliest pending instant (recomputed on every
  change or wake-up, and at least every 30 s, so clock jumps and system sleep are
  noticed), then claims the occurrence and advances the schedule in one
  transaction, and runs the turn with a fresh grant holding exactly the
  schedule's capabilities. The occurrence id is `occ_<schedule>_<instant>` (plus
  `_m` for run-now) and is the turn's idempotency key: an instant is claimed at
  most once, even across restarts.
- **Time.** `once` (`--in`, `--at`), `interval` (`--every`, elapsed seconds) and
  5-field `cron` (Vixie day-of-month/weekday rule, month and day names) on the
  wall clock of an IANA timezone (default: the owner's). The DST rule: every
  matching wall time fires exactly once. A wall time that exists fires at the
  first instant the clock reads it, so a time repeated by fall-back fires only
  in its first pass (and an hourly cron skips the repeated hour). A wall time
  skipped by spring-forward fires as if the old offset still held, i.e.
  shifted forward by the length of the gap (New York 02:30 becomes 03:30 EDT;
  Lord Howe 02:15 becomes 02:45 +11); if that instant coincides with another
  matching time, they fire once. A relative one-shot (`--in`) keeps its exact
  instant. A brute-force oracle that walks every zone's clock minute by minute
  agrees with this for cron and one-shot specs around every 2025-2027
  transition in 16 zones (`test_cell_clock_oracle`). Timezone edits
  re-interpret `once` and `cron` wall times; intervals are unaffected.
- **Missed, overlapping and crashed runs.** An instant that is not the latest
  pending one, predates the daemon's start or is over 60 s old is missed: the
  schedule's policy runs the latest one once, marked late
  (`run-latest-once-late`, default), or records it `skipped` (`skip`). Every
  occurrence records `missed_count`, the scheduled instants it accounts for that
  never ran: 0 for an on-time run, the earlier instants of a stretch whose
  latest ran late (with `missed_from`, the first of them), the whole stretch for
  a `skip` record; the late turn is told how many were missed. Instants (and
  run-now requests) that come due while the same schedule is still running are
  recorded `skipped` with reason `overlap` as they arrive, each under its own
  occurrence id (the turn runs on a helper thread while the loop watches that
  schedule); a stretch the loop could not see in time (clock jump, sleep) gets
  one record for its latest instant with its count. After a crash (even `kill -9`), a
  run that was in flight becomes `uncertain` if a tool had started, else
  `failed`, and is never run again; other schedules continue.
- **Chat tools.** `schedule_create`, `schedule_list` and `schedule_update`
  (pause, resume, edit, run_now, remove) need `schedule.read`/`schedule.write`.
  A schedule made in a turn holds at most that turn's capabilities (default: the
  turn's minus `schedule.*`); a turn can only change schedules whose capabilities
  it holds. Schedules are found by id or exact name. A schedule the owner creates
  (`schedules create`) gets what an owner chat turn gets (learning, long-turn, web and
  every configured MCP server included), unless `--capabilities` narrows it. `created_by` names who
  wrote the prompt its runs follow: `owner`, or `turn:<id>` for a conversation that
  created the schedule or later rewrote its prompt (an owner `schedules edit
  --prompt` makes it `owner` again). A turn that has read text the cell did not
  write (see Governance) cannot rewrite the prompt or name of an owner-written
  schedule. Names are one line, and a run's header always ends before the prompt.
- **Tool arguments.** Unchanged Grail refuses a tool call whose streamed
  argument string is not a JSON object ("Tool arguments must be a valid JSON
  object."), and models stream no argument at all for a tool that requires
  nothing, so every tool the cell advertises requires one: `list_files` a
  `path` (`.` for the root), `schedule_list` a `schedule_id` (`all` lists) and
  the bridge's `brainstem_agent_status` an `intent`. Owner calls may omit an
  argument that has a default, and a JSON `null` for an optional argument
  means "not given".
- **launchd.** `service install` writes `~/Library/LaunchAgents/com.brainstem-agent.cell.<home hash>.plist`
  (RunAtLoad, restart on crash) and runs `launchctl bootstrap gui/<uid>`; `uninstall`
  boots it out and removes the file. `BRAINSTEM_AGENT_LAUNCH_AGENTS` and
  `BRAINSTEM_AGENT_LAUNCHCTL` redirect both (tests never touch the real domain).

## Learning cell (skills, profile memory, session search, context files)

```sh
python3.11 -m brainstem_agent chat "Save how you did that as a skill called make-todo-list" --session <id>
python3.11 -m brainstem_agent skills list --json
python3.11 -m brainstem_agent skills approve make-todo-list --version 2
python3.11 -m brainstem_agent profile list --json
python3.11 -m brainstem_agent memory --scope profile --search "Lisbon" --json
python3.11 -m brainstem_agent sessions search "offsite Lisbon" --json
python3.11 -m brainstem_agent sessions forget <session_id> --json
python3.11 -m brainstem_agent context "Deploy to staging" --json
```

`skills` also takes `show`, `history`, `approve`, `reject`, `disable`, `enable`, `edit`,
`import`, `export`, `share`, `unshare` and `delete` with a skill name; a pending version is
approved only by its number (`--version`). `profile` also takes `add --text ...`,
`edit <fact_id> --text ...` and `forget <fact_id>`. `memory` takes `--scope` (`all`,
`workspace` or `profile`) and `--search`. `sessions` without an action lists sessions;
`sessions forget` blanks a session's turns and inbox answers. `context` previews the learned
context a message would be offered.

What the cell learns is data it offers the model; it is never executed and never grants
anything. New capabilities (in `DEFAULT_CAPABILITIES`, not in `CORE_CAPABILITIES`):
`skills.read` (the skill index and `skill_view`), `skills.write` (`skill_save`) and
`sessions.read` (`session_search`). `remember` gained `scope` (`workspace` default, or
`profile`); `recall` and `forget` cover both scopes.

- **Retrieval first.** One ranking core (`retrieval.py`) serves facts, profile facts,
  skills, instruction-file sections and the fallback session engine: Okapi BM25 with the
  Lucene IDF (positive even in a one-item collection, unlike FTS5's classic IDF), a match
  gate (at least 15% of the request's terms and half the best score), then recency (30-day
  half-life) and usage (recall hits, skill loads) as small signals. The parameters were
  chosen offline on a labelled evaluation set, `runtime/tests/retrieval_eval.py` (three
  workspaces and a profile, 45 facts, 17 skills, 30 past turns, 42 context and 18 session
  queries with stale facts, look-alikes in other workspaces, paraphrases, deleted,
  quarantined and disabled items). On its held-out split the shipped ranking offers 96%
  of the relevant facts and skills at 60% precision (textbook BM25 without the gate:
  100% at 7%); session search reaches MRR 0.94 (keyword overlap: 0.80), with FTS5 and the
  fallback ranking identically. Misses are vocabulary gaps ("bill" vs "invoice");
  `recall`, `skill_view <keyword>` and `session_search` cover them.
- **Context budget.** Each bind offers one learned-context block (`knowledge.py`): at most
  **6,000 characters** for workspace instructions, profile, memory and the skill index
  together, labels and notes included. Shares 2,400 / 900 / 1,500 / 1,200 (maxima 4,000 /
  1,500 / 3,000 / 2,400); a section that needs less leaves its share to the others in that
  order. The session pointer and the newlines that join sections are set aside first; when
  what is left is below the shares' sum, sections give way in reverse order (skills first),
  and no section exceeds its allowance, so the block never passes 6,000 characters (a
  seeded sweep of 400 random layouts checks it). Profile facts are offered whole (best
  first); workspace facts and skills only when they match the request. Whatever does not fit is dropped whole and counted
  (`(+N more not shown; recall searches every saved fact.)`); an instruction file that does
  not fit keeps its opening section and the most relevant headed sections, and names the
  omitted ones. Everything is read from the store at bind time, so a deleted, disabled or
  quarantined item can never reappear. Each turn's evidence carries `context` (per-section
  need, allowance, characters, shown and omitted ids, truncation), never the text.
- **Skills** are store rows (`skills`, `skill_versions`): name, one-line description, when to
  use, steps; every version is kept with its author (`owner` or `model`), session, turn,
  workspace and time. `skills show`/`export` render markdown with frontmatter; `edit` and
  `import` accept it (keys such as `tools` or `capabilities` are ignored). The model finds
  skills in the `<skills>` index and loads one with `skill_view` (its receipt proves the
  load); `skill_save` creates or adds a version.
- **Governance.** Owner-made versions are `approved`. Model-made ones are `unreviewed` and
  offered with an `[unreviewed]` label. A model save is *tainted* when an earlier tool in the
  same turn returned text the cell did not write (file reads, shell output, searches,
  memories, other skills); a tainted save the owner did not ask for is **quarantined**: a new
  skill is stored `quarantined`, a new version of an existing skill becomes its *pending*
  version, and neither is offered until the owner approves it. `skills approve <name>`
  approves the offered version; a pending version is approved only by its number
  (`--version N`, after `skills show <name> --version N`), and one proposed before the
  current version (an owner edit came later) is refused; `reject` discards it. Approving,
  sharing or editing never approves a pending version, and approval never re-enables a
  disabled skill.
- **The owner's request.** "The owner asked" means an explicit request in the owner's own
  words, never the bare word "skill": a saving verb (save, turn, make, record, update,
  add, ...) whose object is a skill ("save how you did that as a skill", "turn this into a
  skill", "update the make-todo-list skill", "the X skill should ..."), not negated ("don't
  save ...") or asked about the past ("did you save ...?"), and outside quoted or pasted text
  (multi-word quotes, code, `>` lines). Mentions are not requests: "summarize skills.md",
  "what skills does it list?", "my skill level". A request that names skills ("... called
  X") covers only those names; one that names none covers the first skill the turn saves.
  A scheduled run's prompt counts only while the owner wrote it (`created_by` `owner`).
  Model skills live in the turn's workspace; only the owner can `share` one with every
  workspace, and approve, disable, edit, export or delete. A disabled skill is never offered
  and cannot be changed from a conversation; `delete` removes every version's text. Nothing
  in a skill is parsed for tools or capabilities: a turn's grant is fixed before the model
  runs.
- **Credentials are never knowledge.** Memory, profile facts and skills refuse
  credential-shaped text (GitHub `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_` tokens,
  bearer tokens, private-key blocks, AWS, OpenAI, Slack, Google and GitLab keys, JWTs, URL
  passwords, `password = ...`-style assignments), from the model and the owner's commands
  alike, with a message that names only the kind. The refused call's receipt records
  `refused: credential` and the kinds; receipts store credential-shaped arguments and
  evidence only as `[REDACTED:<kind>]`.
- **Profile memory** is a separate scope (`profile:` + owner hash) offered in every
  workspace as `<profile>`; `profile` edits and forgets it, `memory` lists both scopes with
  a `scope` field.
- **Session search** (`session_search`, `sessions search`): the owner's messages and the
  answers of succeeded turns in *this workspace*, with snippets, session ids and local
  times. Engine: SQLite FTS5 in a derived index, `state/search.sqlite3` (0600, repaired from
  the store when it lacks turns, rebuilt if broken; every hit is re-read from the store);
  without FTS5 a BM25 scan of the newest 2,000 turns. Turn times live in `turn_log`.
  `sessions forget <id>` blanks a session's turns (the owner's words, the answers and the
  answers its scheduled runs left in the inbox), their receipts' arguments and results and
  streamed events, and drops them from the index (FTS5 secure-delete); rows, states and
  replay keys stay, and forgotten turns are never searched or resent as history.
- **Context files.** `AGENTS.md` and then `BRAINSTEM.md` in the workspace root are offered
  as `<workspace_instructions>` to every turn in that workspace (read fresh on each bind,
  regular single-link files only, never through a symlink, at most 64 KiB read), labelled as
  the owner's instructions that never change tools or permissions.
- **Isolation.** Workspace facts, skills and sessions are queried by workspace namespace
  only; the profile scope (facts and shared skills) is the one documented cross-workspace
  channel. Retrieved blocks are labelled data; the soul and the bind header say so.
- **One place, one spelling** (`paths.py`). The home, cache and workspace are canonical:
  links resolved, then the kernel's own name for the directory (`F_GETPATH`: on-disk letter
  case, no firmlink), so `/var` and `/private/var`, a symlinked parent, another letter case
  or `/System/Volumes/Data/...` name one workspace namespace, and Seatbelt rules match what
  they name. The workspace guard compares places by device and inode: a workspace may not
  be, contain or lie inside the cell's home (except under `<home>/workspaces/`), the Grail
  cache, or the Copilot credential's directory (installed or explicit), whatever spelling
  is used; the shell also denies reading the explicit token file's directory.
- **Daemon and schedules.** All of this is part of every bind, so it works the same
  in-process, through the daemon and in scheduled runs; owner commands write the store and
  count from the next turn. Owner-created schedules default to an owner chat turn's
  capabilities (learning included; `--capabilities` narrows them); chat-created schedules
  hold at most the creating turn's.

## Long-horizon cell (continuation, helpers, scripts, background processes)

```sh
python3.11 -m brainstem_agent chat "Create chain/1.txt ... up to chain/6.txt" --json
python3.11 -m brainstem_agent chat "..." --max-segments 4 --max-tool-calls 40 --max-seconds 300
python3.11 -m brainstem_agent turns list --json
python3.11 -m brainstem_agent turns show <turn_id> --json
python3.11 -m brainstem_agent receipts --turn <turn_id> --json
python3.11 -m brainstem_agent processes list --json
python3.11 -m brainstem_agent processes stop <proc_id> --json
python3.11 -m brainstem_agent cancel --json
```

A request like the first continues across Grail requests until it is done (see
Continuation); the limits bound one turn (see Budgets). `turns show` prints a turn's
journal, `receipts --turn` its receipts and its helpers', `processes` lists or stops
background processes, and `cancel` cancels the daemon's active turn.

- **Continuation.** Unchanged Grail runs at most three tool rounds per `/chat/stream`
  request and then forces one tools-disabled answer. The cell counts the rounds in the
  stream (Grail emits one `agent` event per tool round; a real-core test drives unchanged
  Grail's loop to prove the rule). A request that used all three rounds was cut off, so the
  turn continues with another request, a **segment**: the same session, a fresh grant with
  exactly the turn's capabilities and workspace, the owner's request resent, and a
  continuation instruction carrying the **journal** of every tool call made so far (tool,
  arguments, result; oldest results are shortened, never dropped, within 24,000
  characters; inner script calls included), rendered as text because Grail's history
  accepts only string messages. A segment that ends before the round limit is the model's
  final answer. The session keeps only the owner's words and that answer. Skill governance
  reads the owner's original words for every segment, so "... save how you did this as a
  skill called greet-file" still counts after three rounds of file and shell work.
- **The model's voice.** The continuation text and its journal are always a user-side
  message from the cell. The assistant's turn of a continuation holds only what the model
  itself said at the step limit; Grail's stand-in for an empty answer ("I couldn't finish
  that within the available tool steps.") and a rejected answer are never resent in the
  model's voice (the owner's request then travels in the continuation text alone). **Fake
  tool logs:** an answer that writes tool calls out in the journal's format (`4. write_file
  {...} -> ok`) or repeats the cell's continuation text, with no receipt of this turn (or
  its helpers) behind a claimed call, is never accepted: the next segment says those calls
  did not run, and at the last allowed step the turn ends `partial` ("no receipt"). The
  segment is journaled `imitated` with the claims. An honest answer that quotes calls
  that really ran is accepted as is.
- **One request, always.** Every segment is fitted to one Grail request before it is
  journaled or granted: the session's oldest turns give way first (a session longer than
  128 messages or 256 KiB keeps its newest turns), then a continuation's repeated request
  and its journal's oldest results shrink; a continuation that still cannot fit ends
  `partial` (limit `size`).
- **Grail's logs.** Grail copies every tool result into its `agent` frames and the done
  frame's `agent_logs`, which the RAPP/1 envelope bounds (256 lines, 8 KiB a line, 64 KiB).
  Before the strict adapter reads a stream, the host bounds them deterministically: a line
  keeps 1,500 characters and says `...[N more characters]`, the newest lines are kept
  within 12,000 characters and 200 lines after a first line `[Brainstem Agent: N earlier
  log lines omitted]`, and each run of streamed `delta` frames (never the answer) is merged
  into one. A large file, script output or process log never fails a finished request;
  the model always receives the full tool result.
- **Budgets** (documented defaults; environment `BRAINSTEM_AGENT_MAX_SEGMENTS`,
  `_MAX_TOOL_CALLS`, `_MAX_SECONDS`, `_MAX_CHILDREN`, `_MAX_PARALLEL`, `_MAX_DEPTH`; chat
  flags override): 8 segments (Grail requests), 100 tool calls (inner script calls
  included), 900 s of wall time (helpers included); helpers: 6 per turn, 3 at once, depth
  1, and each helper 4 segments, 40 tool calls and at most 300 s. Hard bounds: 32 segments,
  500 tool calls, 3,480 s (a grant lives at most an hour, and every segment's grant outlives
  its turn's time limit by 120 s), 16 helpers, 4 at once, depth 2. The last allowed segment
  is told it is the last. A limit ends the turn with state `partial` (exit 5): `ok` false,
  no `response`, and a `partial` object and `error` text that name the limit, list the tool
  calls that succeeded (done) and failed, name calls the limit interrupted (their effects are
  uncertain) and quote the model's last words (not done). A spent tool-call budget refuses
  further calls (receipt `denied`, reason given to the model); the wall-time limit stops the
  running segment (grant, tools, worker). The store records the chat as `failed` (never
  success) and the journal as `partial`; a replay reports `partial`.
- **Helpers** (`delegate_tasks`, capability `agents.delegate`). Each task runs as a child
  turn on its own **fresh Grail worker** with its own session and empty history (only its
  task), in the parent's workspace, with the capabilities it declares **intersected with the
  parent's** (default: the parent's; `agents.delegate` is removed at the depth limit), and
  continues across segments like any turn. **Depth** (`BRAINSTEM_AGENT_MAX_DEPTH`, 0-2,
  default 1) is exactly the number of helper levels below an owner's turn: 0 offers no
  `delegate_tasks` at all, 1 gives helpers that are never offered it, 2 lets those helpers
  delegate once more to helpers that are not (at most 4 + 16 helper workers alive at once
  at the hard bounds). Helpers run in parallel (bounded by a semaphore);
  the parent receives one joined answer with every helper's state and answer, failures
  included, and the call's evidence gives wall and serial seconds. Helper receipts carry
  the helper's turn id; `receipts --turn <parent>` returns them with `parent_turn`. A
  helper's task was written by the model, so it never counts as the owner asking for a skill.
  Cancelling the parent (Ctrl-C, SIGTERM, `cancel`, daemon stop) stops every helper's
  worker in parallel.
- **Programmatic tool calls** (`run_script`, capability `scripts.run`). A short Python
  script (standard library, this interpreter with `-I -S`) runs inside Seatbelt with **no
  network**, the workspace **readable but not writable**, and a private scratch directory.
  It calls cell tools with `call(tool, **arguments)` (helpers `read_text`, `write_text`)
  over a pipe to the host: the broker makes each inner call on behalf of the `run_script`
  call, with its own receipt (`<call_id>.<n>`), the grant resolved again (revoked, expired,
  cancelled or another generation refuse), the turn's capabilities and an allowlist
  (`read_file`, `write_file`, `list_files`, `recall`, `skill_view`, `session_search`).
  `read_text(path)` returns a file's exact text or raises `ToolError` when `read_file`
  could return only part of it (over 64,000 characters or 256 KiB, or not UTF-8), so a
  script never rewrites a file from a clipped copy.
  Bounded: 50 inner calls, 30 s default (120 s max), 20,000 characters of code, 64,000
  characters of output. Every effect of a script is therefore a receipted tool call.
  When the interpreter itself is installed under the home (a pyenv or uv Python), its
  standard library directory is readable to scripts so that it can start; nothing else of
  the home is.
- **Background processes** (`process_start`, `process_status`, `process_read`,
  `process_write`, `process_stop`; capability `processes.run`). `/bin/sh -c` in the shell's
  Seatbelt profile (no network, writes only in the workspace), in its own supervised
  process group, owned by its workspace. **Daemon-managed**: a process outlives the turn
  that started it while the daemon runs; without a daemon it stops when the command ends.
  Bounded: 4 running per host, the first 1 MiB of output kept (more is counted), 8,000
  characters per write. `stop` (and the end of an in-process command) stops them all.
- **Progress.** `chat` streams progress to stderr: JSON lines with `--json` (`turn.started`,
  `segment.started`, `tool.finished`, `segment.finished`, `turn.continuing`,
  `child.started`, `child.finished`, `limit.reached`, `turn.finished`), short text lines
  otherwise; `--quiet` silences them. Through the daemon the CLI polls `POST /v1/progress`.
  The final JSON's `evidence.long_turn` has every segment (rounds, calls, exhausted, seconds),
  every helper (state, seconds, segments, worker) and the counts.
- **Cancellation.** Ctrl-C, SIGTERM or `cancel` revokes the current segment's grant,
  cancels its in-flight tools (a running script's group is killed), stops every helper and
  the worker, all within 5 s (measured by the unit specs for a continuation, helpers and a
  script; a real worker group's stop is measured by the real-core A8 specs).
- **Settling** reads every receipt of a turn (and of each helper), however many, so a call
  whose outcome could not be recorded keeps its turn `uncertain` even after hundreds of
  calls.
- **One durable journal** (`turn_steps` in the single store, schema v2 extended additively
  like the scheduling and learning tables): the turn (budget, then its outcome), each segment (journaled
  `running` *before* its Grail request is sent, with the count of tool calls the turn had
  started so far, then its rounds, calls, answer and calls' results), and each helper
  (journaled before any helper starts). Receipts journal every tool call and inner call
  before it runs (a call whose receipt cannot be written is not run) and after it ends;
  `processes` journals each background process as `starting` before it launches. `turns
  show` prints it all.
- **Crash semantics.** After a host or daemon dies (even SIGKILL), the lifeline watchdog
  kills every recorded group (Grail workers, helpers' workers, shell commands, scripts,
  background processes) and the next command's recovery settles the journal without
  re-running anything: a turn or helper whose tree started any tool is `uncertain`, one
  that provably started none is `failed` (the chat gets the same state; chats without a
  journal stay conservatively `uncertain`); a segment is `uncertain` when the turn started
  a tool after it was sent, else `failed`; started receipts become `uncertain` while
  finished ones stay as they were (a completed effect is never repeated); background
  processes become `lost`. Replaying the turn's idempotency key reports that state and
  never calls Grail. Crash injection (`BRAINSTEM_AGENT_CRASH_AT=<point>[#n]`: only
  `segment.started`, `segment.finished`, `child.started`, `child.finished`,
  `script.inner`, `process.started`, `turn.finishing`) and kills while a tool, helpers, a
  script or a process run prove each boundary (`test_cell_durability`,
  `test_real_longturn`). The hook is inert unless that variable is in the environment the
  owner starts the host or daemon with: nothing a model says or writes, no workspace file
  (`.env`, `AGENTS.md`) and no daemon request can arm it, no worker, shell command, script
  or process inherits it, and the launchd service never copies it.
- **Authority.** Continuations, helpers and inner calls never exceed the parent turn's
  capabilities or workspace (each segment and helper gets its own grant, bound to its own
  worker generation, revoked when it ends). An owner chat turn gets `agents.delegate`,
  `scripts.run` and `processes.run` by default, and so does a schedule the owner creates
  unless `--capabilities` narrows it (owner schedules equal owner chat defaults); a
  schedule a turn creates holds them only when that turn names them, and
  a scheduled run's helpers and segments stay within the schedule's capabilities.

## Reaching cell (web fetch, web search, MCP servers, egress policy)

```sh
python3.11 -m brainstem_agent chat "Read https://kody-w.github.io/brainstem-agent/ and tell me what it says. Cite it."
python3.11 -m brainstem_agent tool web_search --arguments '{"query": "mitochondria"}'
python3.11 -m brainstem_agent tool mcp__notes__note_get --arguments '{"key": "k"}' --capabilities mcp.notes
python3.11 -m brainstem_agent doctor --json
python3.11 -m brainstem_agent status --json
python3.11 -m brainstem_agent mcp list --json
python3.11 -m brainstem_agent mcp status notes --json
python3.11 -m brainstem_agent mcp trust notes --json
python3.11 -m brainstem_agent egress log --limit 20 --json
```

`doctor --json` includes `reach` (the config path, MCP servers and problems), and `status
--json` (through the daemon) the MCP server states. `mcp list` shows the configuration (never
a URL path, argument or environment value). `mcp status` (optionally for one server) reports
the daemon's servers, or else starts, checks and stops them. `mcp trust` pins a server's
current tool definitions. `egress log` shows recent outbound requests, without their queries.

One owner-editable file, `$BRAINSTEM_AGENT_HOME/reach.json` (read fresh on every use; the
worker, shell, scripts and processes can neither read nor write the home):

```json
{
  "web": {"allow_domains": [], "deny_domains": ["example.com"], "max_requests_per_turn": 20,
          "max_bytes_per_turn": 5000000, "max_page_bytes": 2000000, "timeout_seconds": 15,
          "max_redirects": 5, "search_provider": "wikipedia", "search_lang": "en",
          "search_key_file": "", "search_key_env": ""},
  "mcpServers": {
    "notes": {"command": "/path/to/python3", "args": ["/path/notes_server.py"],
              "env": {"NOTES_TOKEN": "..."}, "allow": ["note_*"], "deny": ["note_delete"],
              "effects": {"note_put": "write"}, "timeout_seconds": 30,
              "sandbox": {"network": "none", "readable": ["/path"], "writable": ["/path/data"]}},
    "remote": {"url": "http://127.0.0.1:8765/mcp", "bearer_token_file": "~/.config/remote.token"}
  }
}
```

- **Tools.** `web_fetch` (`web.fetch`: a public http(s) URL -> readable text, HTML reduced to
  its visible text without scripts, styles and navigation; title, final URL, fetch time,
  status and the content's SHA-256; long pages in parts of 5,000 characters, `offset`
  pages from the cell's copy without a new request) and `web_search` (`web.search`: titles,
  URLs and snippets). Each configured MCP server `<s>` adds capability `mcp.<s>` and its
  allowed tools as `mcp__<s>__<tool>` (results paged with `result_offset`). An owner chat
  turn holds `web.*` and every configured `mcp.*` by default, and so does a schedule the
  owner creates unless `--capabilities` narrows it; a schedule made in a turn and a helper
  hold at most the creating turn's (unchanged rules). Every result stays under 6,000
  characters and about 40 lines of at most 1,500 characters (`LOG_LINE_CHARS`): compact for
  the model, and inside the line bound of the one log-size mechanism (Grail copies every
  result into its logs, which the host bounds before the strict adapter; see Long-horizon).
- **Egress (the host makes every request; nothing sandboxed has network).** Per hop, redirects
  included: http(s) only, no credentials in the URL, one spelling of the host (an IP literal
  in canonical form, else the lowercase IDNA name without a trailing dot; a `%` zone id or
  escape, and numbers spelled another way such as `0177.0.0.1`, `127.1`, `2130706433` or
  `0x7f.1`, which parsers read differently, are refused before any lookup), the owner's
  `allow_domains` (empty: any) and `deny_domains` (normalized the same way, so `bücher.example`
  also covers `xn--bcher-kva.example`; a domain covers its subdomains; deny wins), then one
  DNS lookup whose every answer must be public: not loopback, private, link-local, CGNAT,
  multicast, reserved, unspecified, site-local or a cloud metadata address. An IPv4-mapped
  address is judged by its IPv4 address; NAT64, 6to4, Teredo and the other IETF special-use
  blocks are refused, the same on every Python patch release. The connection goes to that
  checked address (TLS still verifies the name; no proxy), so an answer that changes after
  the check is never used. A caller's headers (a search provider's key) go only to the first
  hop's origin, never across a redirect to another. Per turn (helpers count toward their
  root turn): `max_requests_per_turn` requests, `max_bytes_per_turn` bytes; per request
  `timeout_seconds`, `max_page_bytes` (the rest is cut and said so) and text types only.
  Every request, MCP HTTP included, is appended to `state/egress.jsonl` (0600, rotated at
  1 MiB; `egress log` prints it) and to the call's receipt: time, turn, tool, method, host,
  address, port, path **without the query** (for MCP HTTP no path at all, since an owner's
  URL may carry a key there), status, bytes and seconds; never a header.
- **Search providers** are functions `provider(query, limit, get, key, lang=...)` in
  `organs/web.py` `PROVIDERS`, returning `[{"title", "url", "snippet"}]`; `get(url, headers)`
  makes one policed request. `wikipedia` (default) needs no key; `brave` reads its key from
  `search_key_file` or the environment variable named by `search_key_env`, sends it only in
  a header, and the key is never shown to the model, logged or stored.
- **MCP servers** (the common `mcpServers` shape; `disabled: true` skips one). Transports:
  stdio (newline-delimited JSON-RPC) and streamable HTTP (POST; JSON or SSE answers;
  `Mcp-Session-Id`; loopback allowed because the owner wrote the URL; an optional bearer
  token read from its file at connect time, never shown, logged or stored; a 404 for the
  session, as after a server restart, re-initializes once and retries). The cell completes
  `initialize`, lists tools (paged), filters them with `allow`/`deny` (fnmatch patterns),
  reduces each input schema to what it validates, and maps effects: the owner's `effects`,
  else `read` for tools the server marks read-only, else `external`. Server requests other
  than `ping` are refused; values of a server's `env` (8+ characters) and its token are
  redacted from results.
- **Pinned tool definitions (tool poisoning, rug pulls).** The first time a server lists its
  tools, the SHA-256 of each definition (name, description, input schema, annotations) is
  pinned in `state/mcp-pins.json` (0600). Later a tool whose definition changed, or that is
  new, is withheld: neither advertised nor callable, and `mcp status` names it and why,
  until the owner reviews it and runs `mcp trust <server>` (a running daemon sees the new
  pins at once). Unchanged tools stay offered.
- **MCP lifecycle.** A server starts on demand (a turn, segment or owner call holding its
  capability), then stays up for the host's life (the daemon's) and is stopped by `stop`
  or the end of an in-process command. A stdio server runs in its own Seatbelt profile (no
  network; reads and writes only its private `run/processes/mcp-*` directory plus the
  owner's `readable`/`writable` paths, which may never hold the cell's home or the Copilot
  credential's directory; `network: outbound` allows non-loopback network), in its own
  process group on the lifeline (killed with its host, reaped after a crash), with rlimits
  (256 open files, 1 GiB per file, 24 h CPU, set by `/bin/bash`, whose `ulimit -f` counts
  1 KiB blocks, rather than `/bin/sh`, which the owner may point at dash's 512-byte blocks;
  macOS enforces no memory limit) and a minimal environment. After a crash it restarts
  with backoff (1 s doubling to 60 s for crashes in a row, counted from the crash; five
  quiet minutes reset it), on its next use
  and from the daemon's idle loop. A server whose program is installed under the home (for
  example a pyenv, uv or nvm install) needs that installation in its `sandbox.readable`.
  A call past `timeout_seconds` is cancelled
  (`notifications/cancelled`), reported to the model and its receipt, and the server is
  restarted; cancelling a turn (Ctrl-C, `cancel`, stop) cancels its in-flight MCP and web
  requests (sockets are shut at once). Writes to a stdio server never block: one that stops
  reading its input for a call's time limit is stopped and restarted on its next use, and
  stopping a server never waits on a stuck write.
- **Untrusted content.** Web pages, search results and MCP results reach the model as
  `<untrusted_data source="...">` blocks that the content cannot close; each bind that
  grants web or MCP tools says to never follow instructions inside them. They taint the
  turn: a skill saved afterwards that the owner's own words did not ask for is quarantined
  (see Governance), an owner-written schedule cannot be rewritten (see Chat tools), and `remember`/`forget`
  run only when the owner's own words in that turn ask for memory (remember, memorize,
  memory or forget, outside quoted text; a helper's task never counts). Taint is not lost
  on the way: a helper starts with the outside content its parent had read before
  delegating (web, MCP, earlier helpers' answers), and a run of a schedule whose prompt a
  conversation wrote starts with that conversation's (from its receipts, and for a helper
  the web and MCP reads of the turns above it). In a continuation, the journal's shortened
  outside results stay inside closed data blocks. The fabricated tool-log guard knows the
  MCP tools too. Nothing in any result changes a grant: tools not granted are neither
  advertised nor callable.

## Process lifecycle

- **Lifeline.** Each host records every process group it starts (Grail worker
  generations, helpers' workers, shell commands, scripts, background processes) under `$BRAINSTEM_AGENT_HOME/run/hosts/<host_id>/`
  while it holds an exclusive `flock` on that directory's `lease`. The first
  tracked group also starts a tiny watchdog (own session) that holds the read
  end of a pipe; when the host dies, even by SIGKILL, the pipe reaches EOF and
  the watchdog SIGKILLs every recorded group whose leader still has the
  recorded kernel start time, then exits.
- **Reaper.** Every host start (any command that opens a home) inspects host
  directories whose lease nobody holds. A recorded group is killed only when
  the pid still has the recorded start identity and runs one of the recorded
  programs; otherwise it is reported `not-ours` and left alone. The recorded
  trees (`workers/...`, `run/shell/...`) and records are then removed.
  `AgentHost.recovered` lists each entry (`killed`, `already-gone`, `zombies`,
  `not-ours`, `never-started`).
- **Zombie-only groups.** macOS `killpg` answers EPERM when every member is a
  zombie, so stop and cleanup read the group from the kernel (libproc), reap
  their own leader and report `group_state` `alive`, `zombies` or `gone`.
- **Signals.** SIGINT and SIGTERM during `chat` or `tool` only set a cancel
  flag: a running shell tool's group is killed, the worker is stopped, the
  command exits 4, and a cancel that arrives before the turn starts (for
  example while waiting for the per-home lock) reserves nothing. The work runs
  on a helper thread while the main thread waits on an Event, so handlers run
  promptly on CPython 3.11 and 3.13.
- **Launch gate.** Every tracked program (Grail worker, shell command) starts
  as a tiny gate that execs it only after the host has recorded its pid; a host
  killed between `Popen` and the record leaves a gate that reads EOF and exits,
  never an unrecorded, orphaned program (about 10 ms per launch).
- **Tool cleanup.** A turn returns only after its cancelled tools have stopped
  (no receipt stays `started`), `AgentHost.close()` stops in-flight tools
  before it returns, and a finished shell command leaves nothing in its group.
  A tool call whose receipt cannot be written is not run (HTTP 503 to the
  bridge) and its turn fails; one whose outcome cannot be written (after a few
  retries) returns its real result to the model, keeps its receipt `started`
  (recovery marks it uncertain) and makes its turn `uncertain`, never
  `succeeded`. A finished turn's grant bookkeeping is dropped from the broker
  once the grant is revoked, so nothing per turn accumulates in a daemon.
- **First use.** Opening a store takes a short exclusive `flock` on the state
  directory, so concurrent first opens of a new store or home never fail.
- **Limit.** A shell descendant that calls `setsid()` leaves its process group,
  so no group kill (tool end, cancel or lifeline) reaches it. It keeps running
  inside the same no-network Seatbelt sandbox: no writes outside the workspace,
  no reads of the owner's home or the cell's state. The reaper also never kills
  a leftover whose program changed by `exec` (for example `exec sleep`); the
  watchdog, which checks the exact start time only, does.
- **Limit: reverse DNS.** The cell's own servers (broker, daemon) bind without asking DNS
  about their address, but the unchanged Grail's web server (werkzeug's, a stdlib
  `HTTPServer`) looks up the name of `127.0.0.1` every time a worker starts. macOS normally
  answers that at once from the `127.0.0.1 localhost` line in `/etc/hosts`; on a Mac whose
  reverse lookup of `127.0.0.1` is slow, every worker start waits for it.

Environment: `BRAINSTEM_AGENT_HOME` (default `~/.brainstem-agent`, owner-only),
`BRAINSTEM_AGENT_CACHE` (default `$BRAINSTEM_AGENT_HOME/cache`),
`BRAINSTEM_AGENT_WORKSPACE`, `BRAINSTEM_AGENT_MODEL` (default `auto`),
`BRAINSTEM_AGENT_GRAIL_SEED` (verified local seed instead of codeload),
`BRAINSTEM_AGENT_GITHUB_TOKEN_FILE` (explicit credential; no fallback) and
`$BRAINSTEM_AGENT_HOME/reach.json` (web egress policy and MCP servers, see above),
`BRAINSTEM_HOME` (installed brainstem, default `~/.brainstem`; its
`src/rapp_brainstem/.copilot_token` is read read-only and handed to workers as
`GITHUB_TOKEN`). Credential values, worker keys and grant handles are never
printed, logged, persisted or returned.

## Tests and evidence

The unit tier, from the repository root:

```sh
PYTHONPATH=runtime python3.11 -m unittest discover -s runtime/tests -q
```

The gated tiers also need `runtime/tests` on `PYTHONPATH`. The real-core line starts the real
Grail without inference; the live suites spend real Copilot inference:

```sh
export PYTHONPATH=runtime:runtime/tests
BRAINSTEM_AGENT_REAL_CORE=1 python3.11 -m unittest test_real_core test_real_lifeline test_real_daemon test_real_learning test_real_longturn test_real_reach
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_daemon
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_learning
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_longturn
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_reach
```

They use about 12 (`test_live`), 10 (`test_live_daemon`) and 13 (`test_live_learning`) live
turns, and about 12 (`test_live_longturn`) and 6-10 (`test_live_reach`) Grail requests.
Setting `BRAINSTEM_AGENT_LIVE_REQUEST_BUDGET` (for example to `12`) caps every live chat's
Grail requests in one test process. The offline retrieval evaluation and the evidence
runner:

```sh
python3.11 runtime/tests/retrieval_eval.py
python3.11 runtime/tests/run_acceptance.py --real-core --live \
  --python-matrix /path/to/python3.13 --matrix-real-core --label NAME --output evidence.json
```

`retrieval_eval.py` takes `--tune` (rerun the grid search) and `--output FILE`.

The unit tier makes no outbound request and needs no credential or Grail download. Tests
that copy the pinned Grail source (some unit specs, and every real-core and live test)
need a verified seed: `BRAINSTEM_AGENT_GRAIL_SEED` names a `rapp_brainstem` directory
(checked against the pinned inventory when used); otherwise the cell's own verified cache
is used read-only (`BRAINSTEM_AGENT_TEST_CACHE`, `BRAINSTEM_AGENT_CACHE`,
`$BRAINSTEM_AGENT_HOME/cache`, then `~/.brainstem-agent/cache`, as `setup` creates it).
Without one they skip and say why. `BRAINSTEM_AGENT_TEST_CACHE` also keeps the real-core
tiers' prepared cache (and worker venv) between runs.

`runtime/tests/mcp_fixture.py` is a small stdlib MCP server (key-value notes and probes, over
stdio, or streamable HTTP with `--http PORT [--token-file F]`; `--variant poisoned` serves
changed tool definitions) used by the E4-E10 specs; point
an `mcpServers` entry at it to try the configuration. The web specs reach a loopback fixture
through injected name resolution, so the unit tier makes no outbound request.
`test_cell_reach_hardening` holds the reaching cell's hardening specs (web and MCP inside
long turns, review findings, owner commands, pinning).

Run the gated tiers with `runtime/tests` on `PYTHONPATH` (as above). Every acceptance test
is tagged with the criteria it proves: A1-A11 (Cell v1), B1-B12 (always-on), C1-C12
(learning), D1-D12 (long-horizon) and E1-E12 (reaching). `run_acceptance.py` writes
sanitized evidence with classes `unit`, `real-core` and `live` and environment
`macos-seatbelt` (`--label` names the run in the evidence; `--only <pattern>` reruns single
tests; `--exclude <module>` leaves a module out and `--merge <evidence.json>` adds the records
and metrics of an earlier run on the same commit).

# Offline M0 harness (still valid unit evidence)

The M0 fixture harness below remains and must keep passing. It does not start
Grail; its limits describe the fixture only, not the Cell v1 runtime above.

## Run

From the repository root, with Python 3.11+ on macOS:

```sh
PYTHONPATH=runtime python3.11 -m unittest discover -s runtime/tests -v
PYTHONPATH=runtime python3.11 -m brainstem_agent fixture
```

The harness has no third-party runtime dependencies. Tests do not need a model,
account, network connection, installed Brainstem, container service or provider
credentials. They use explicitly injected, trusted fixture code and private
temporary directories.

To retain a synthetic evidence report:

```sh
PYTHONPATH=runtime python3.11 -m brainstem_agent fixture --output /your/chosen/new-evidence.json
```

The output file must not already exist. The report says
`stage: offline-contract-verified`, `real_grail_executed: false`,
`native_rapp_activated: false`, and `sandbox_qualified: false`. Its temporary
artifact is removed when the fixture ends; the report retains the measurement,
not an independently available copy of that file.

`--mode live` refuses before execution or evidence output.

## Implemented contract surfaces

- Strict core request/response normalization and bounded SSE parsing, with
  explicit failures instead of accepting error-shaped HTTP 200 results.
- Transactional chat reservations, session ownership, terminal replay and
  history. Repeating a terminal chat key replays the original response even
  when new input differs. The harness partitions these records by both owner
  and workspace; changing the workspace cannot adopt a prior session or replay.
- Separate job admission semantics: an identical key/request reuses the job;
  changed request content conflicts.
- Persisted synthetic grants with owner/workspace/session/run/worker-generation
  bindings, explicit capabilities, expiry and durable revocation.
- One active fixture turn per harness worker, durable outcome storage and
  conservative uncertainty after interrupted dispatch.
- A restricted fixture file writer with no-overwrite/no-leaf-symlink behavior,
  exact effect records, and injected crash-boundary tests.
- A headless CLI that exercises admission, a real **fixture** file write,
  terminal replay and reopening the store.

See [`contracts/m0.md`](../contracts/m0.md) for the shared API and refusal rules.

## What this does not establish

Injected fixture transports execute trusted Python in the test process. The
fixture writer is **not** an OS sandbox. These modules cannot prevent an
arbitrary injected callback from using ambient process authority. A path
allowlist, separate process, random token or local SQLite database does not
establish a production security boundary.

The grant clock is a caller-supplied fixture clock. Its monotonic floor is
process-local, not protected against whole-store/system rollback. Observed
expiry and revocation persist, but production clock authority remains
unqualified.

The store uses local JSON comparison for application requests, not a replacement
RAPP canonicalizer. It does not mint identities, signatures, native frame hashes,
Work completion receipts or an estate's approval. Native RAPP records would need an
authenticated RAPP contract.

The fixture harness alone proves none of the cell's properties (real Grail request
capture, worker isolation, tool-backed inference, process-tree cancellation, memory);
the cell's own unit, real-core and live tests above do. Provider independence, native
RAPP evidence, a Linux sandbox, messaging channels and cloud deployment are not
implemented.

Do not use these fixtures to upgrade the site's capability claims. Do not run stock
installers against existing installations.

## State and recovery

Only an exclusive supervisor may call `recover_interrupted()`. It marks running
chats/jobs **uncertain** rather than rerunning them. Store construction does not
perform recovery, because another worker could still be active.

A file write and a SQLite result commit are not one atomic operation. A crash
between them intentionally leaves an uncertain effect. Inspect/reconcile that
state; never blindly replay it or assume the side effect did not happen.

Use private local storage, not shared/NAS SQLite or copied account directories.
Live secrets, user conversations and provider stores have no role in the test
fixtures.
