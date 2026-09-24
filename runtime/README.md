# Brainstem Agent runtime (experimental, 0.2.0)

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
People use it through an interactive terminal session and an owner-only web companion
([Companion surfaces](#companion-surfaces-interactive-terminal-and-owner-only-web-companion));
agents use the same engine through `--json` and the daemon's documented routes.

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
backend (setuptools) from PyPI.

**Offline install.** Two paths need no network for the runtime itself. With Python 3.11,
pip can build the package with the venv's own setuptools, but only when that setuptools is
70.1 or newer, or the `wheel` package is installed in the venv (Homebrew's 3.11 venvs
qualify; the python.org 3.11 installers bundle an older setuptools, and pip then stops with
"invalid command 'bdist_wheel'"). `pip show setuptools` prints the venv's version, offline:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip show setuptools
.venv/bin/pip install --no-index --no-build-isolation ./runtime
```

Otherwise, and with Python 3.12 or newer (their venvs have no setuptools), the single-file
zipapp is the offline path; it works with any Python 3.11 or newer. Build it once from a
checkout, copy the one file, and run it with any interpreter; on first run it checks every
file against its release manifest and unpacks itself into `$BRAINSTEM_AGENT_HOME/versions/`:

```sh
PYTHONPATH=runtime python3.11 -m brainstem_agent.release --zipapp brainstem-agent.pyz
python3.13 brainstem-agent.pyz setup
python3.13 brainstem-agent.pyz doctor
```

`setup` still needs the pinned core (downloaded from GitHub, or a verified local copy named by
`BRAINSTEM_AGENT_GRAIL_SEED`) and the worker's hash-locked packages (from PyPI, or pip's local
cache).

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

## Companion surfaces (interactive terminal and owner-only web companion)

Two surfaces for people, both mirrors of the same engine: every action either one takes is a
documented daemon route that the CLI reaches too (the table below), so nothing exists only
in a UI. Threat model, hostile tests and contract:
[`contracts/companion.md`](../contracts/companion.md).

### Terminal session

```sh
python3.11 -m brainstem_agent
python3.11 -m brainstem_agent repl --session <session_id>
python3.11 -m brainstem_agent repl --json
```

The first line opens the interactive terminal (the installed command alone, `brainstem-agent`,
does the same; so does `repl`). `--session` continues an earlier session; `--json` is the line
protocol for agents (below).

- **Routing.** With a daemon running, each turn goes through it (`POST /v1/requests` and its
  event stream, exactly what the companion uses); without one, turns run in-process on a
  host the session keeps, so the worker stays warm between turns.
- **Output.** The answer streams as Grail writes it; tools, steps and helpers print one
  progress line each on stderr; a final line gives the engine's state and receipts, and the
  engine's recorded answer is repeated when it differs from what streamed.
- **Keys.** Ctrl-C cancels the running turn (grant revoked, worker group stopped, as in
  `chat`) and keeps the session; Ctrl-D or `/exit` quits.
- **Slash commands.** `/help`, `/new`, `/sessions`, `/resume <id>`, `/history`, `/skills`,
  `/memory`, `/schedules`, `/inbox`, `/status`, `/stop` (cancels whatever turn the daemon
  runs, from any surface) and `/exit`.
- **History.** Typed lines are kept in `<home>/state/repl_history` (0600, never written through
  a symlink) and offered by the up arrow. A credential-shaped line (a token, a key, a
  `password = ...` assignment) is kept in neither: not in the file and not in the terminal's
  own history; the terminal says so.
- **Untrusted text** (answers, tool output, names) is printed with control and C1
  characters, line separators and every Unicode bidi control character (embeddings,
  overrides, isolates and marks) shown as visible escapes (`\x1b`, `\u202e`), so nothing a
  model or a web page writes can drive the terminal or reorder what you read.
- **Streamed text is redacted** the way the recorded answer is, and for every credential
  shape besides, on a rolling buffer: a word appears once it has ended (after a secret's
  name, once its value has), so a secret split across Grail's fragments never shows, not
  even in part.

The JSON line protocol (`repl --json`, `brainstem-agent-repl/1`) reads one line per input:
plain text (as typed) or a JSON object, `{"op": "chat", "text": "..."}`,
`{"op": "command", "text": "/sessions"}`, `{"op": "cancel"}` (read while a turn runs) or
`{"op": "exit"}`. It writes one JSON object per line: `ready` (protocol, mode `daemon` or
`in-process`, session, commands), `delta` (answer text), `event` (a progress event),
`result` (the full turn result, as `chat --json` prints it), `command` (a slash command's
data), `notice`, `error` and finally `bye`.

### Web companion

```sh
python3.11 -m brainstem_agent serve --detach
python3.11 -m brainstem_agent open
python3.11 -m brainstem_agent open --sign-out-all
```

The daemon serves the companion on its own `127.0.0.1` port. `open` prints a one-time
sign-in link, `http://127.0.0.1:<port>/login#<token>`: open it in a browser on this Mac; it
works once, for two minutes, in one tab. `open` never launches a browser itself (a launched
browser's arguments are visible to other users). `--sign-out-all` ends every companion
session and unused link.

- **Sign-in.** The token (256 random bits, held only as a digest in daemon memory) rides in
  the URL fragment, so it is never sent in a request line, a Referer or a log. The login page
  removes it from the address bar and the history entry, exchanges it by `POST`, and receives
  an `HttpOnly; SameSite=Strict` session cookie (named per port) plus a CSRF secret for that
  tab only (`sessionStorage`). Every companion request needs both, because cookies are shared
  by every port of 127.0.0.1; every state change also needs the exact `Origin` and a JSON
  body; fetch metadata must say same-origin; the Host must be exactly `127.0.0.1:<port>`.
- **Sessions** live in daemon memory: idle 1 h, absolute 12 h, at most 8; a restart ends them.
  The daemon comes back on its previous port (`run/port.json`) when nothing else listens
  there, so an open tab keeps its address and says **Daemon restarted** (sign in again with a
  new link) rather than only **Daemon unreachable**; a tab that was signed out says that.
- **Views.** Chat (streamed answers with steps, helpers, receipts and Stop), sessions (resume),
  schedules and inbox (pause, resume, remove), skills (show, approve, reject a pending
  version, disable, enable), memory and profile (edit, forget), MCP servers and tools, the
  egress log and status. The page is plain HTML, CSS and JavaScript shipped in the package
  (`ui/`, about 47 KB): no build step and no external requests.
- **Readiness.** The Status view (and the connection line) shows live versus ready from
  `GET /v1/health`, read-only: every check with its reason and, when it fails, its fix. The
  CLI prints the same report with `api GET /v1/health`, and `status --json` carries it as
  `readiness`.
- **Honest states.** A turn is `queued` (waiting for the worker), `running`, `streaming (not
  final)`, then exactly what the store records: `succeeded`, `partial`, `uncertain`, `failed`
  or `cancelled`; a turn the store records as running that no live request runs is `stale`.
  Each has a text label, its own border and its own symbol, in the sessions list too (it
  labels each session by its last turn). Streamed text of a turn that did not succeed stays
  under "Streamed text (not recorded as an answer)".
- **Untrusted text** is placed with `createElement` and `textContent` only (no links, images
  or HTML parsing), and bidi control characters are shown as escapes, as in the terminal.
- **Hardening** on every response (assets, JSON, event streams, refusals, standard-library
  errors): `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self';
  img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors
  'none'; object-src 'none'; require-trusted-types-for 'script'; trusted-types 'none'`,
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  COOP and CORP `same-origin`, `Cache-Control: no-store`; no CORS grant and no redirect ever;
  JSON escapes `<`, `>` and `&`; Trusted Types make any HTML sink throw.

### Every route, from the CLI

```sh
python3.11 -m brainstem_agent api GET /v1/api
python3.11 -m brainstem_agent sessions show <session_id> --json
python3.11 -m brainstem_agent memory edit <fact_id> --scope workspace --text "a corrected fact"
python3.11 -m brainstem_agent memory forget <fact_id> --scope profile
```

`api METHOD PATH` calls any documented route as the owner (`--body` takes a JSON object for a
`POST`; an event stream prints one JSON line per event). `GET /v1/api` returns this table;
the companion may call only the rows marked yes:

| Route | Companion | What | CLI |
|---|---|---|---|
| `GET /v1/api` | yes | This route table. | `brainstem-agent api GET /v1/api` |
| `GET /v1/status` | yes | Health, workers, active turn, schedules, MCP servers, last errors, companion. | `brainstem-agent status --json` |
| `GET /v1/health` | yes | Liveness versus readiness: every check with its reason and fix. | `brainstem-agent api GET /v1/health` (status --json carries the same report as readiness) |
| `GET /v1/version` | no | The running version, Grail pin, store schema, digests and capabilities. | `brainstem-agent api GET /v1/version` (version --json shows it next to the installed release) |
| `POST /v1/turn` | no | Run one turn and wait for its full result. | `brainstem-agent chat <message> --json` |
| `POST /chat` | no | RAPP/1: exactly response, agent_logs, session_id. | `brainstem-agent api POST /chat --body <json>` |
| `POST /v1/tool` | no | Invoke one cell tool directly as the owner. | `brainstem-agent tool <name> --arguments <json>` |
| `POST /v1/cancel` | yes | Cancel a request ({request_id}) or the active turn ({active: true}). | `brainstem-agent cancel` (the active turn; Ctrl-C cancels a running chat or terminal turn) |
| `POST /v1/progress` | no | Progress events of a /v1/turn request. | `brainstem-agent chat <message>` (progress lines on stderr) |
| `POST /v1/wake` | no | Wake the schedule loop. | `brainstem-agent api POST /v1/wake` (every schedules command that changes a schedule wakes it too) |
| `POST /v1/drain` | no | Finish the running work and start nothing new ({timeout}); {resume: true} undoes it. | `brainstem-agent stop --drain` (upgrade and rollback drain the daemon first) |
| `POST /v1/stop` | no | Stop the daemon and its workers. | `brainstem-agent stop` |
| `POST /v1/requests` | yes | Start a turn ({message, session_id?}); 202 with request_id, state queued. | `brainstem-agent api POST /v1/requests --body <json>` (the terminal sends each message this way) |
| `GET /v1/requests/{request_id}` | yes | A started turn's state and, once finished, its result. | `brainstem-agent api GET /v1/requests/<request_id>` |
| `GET /v1/requests/{request_id}/events` | yes | text/event-stream of a started turn (?after=SEQ): queued, running, progress, answer.delta, request.finished. | `brainstem-agent api GET /v1/requests/<request_id>/events` (the terminal streams it; one JSON line per event) |
| `GET /v1/sessions` | yes | Sessions of the daemon's workspace, each with its last turn's label. | `brainstem-agent sessions list --json` |
| `GET /v1/sessions/{session_id}` | yes | Every turn of a session with its state label, answer and receipts. | `brainstem-agent sessions show <session_id> --json` |
| `GET /v1/journal/{turn_id}` | yes | A turn's journal: segments, helpers, receipts. | `brainstem-agent turns show <turn_id> --json` |
| `GET /v1/receipts` | yes | Receipts (?turn=TURN_ID: that turn and its helpers). | `brainstem-agent receipts --json` |
| `GET /v1/schedules` | yes | Schedules of the workspace. | `brainstem-agent schedules list --json` |
| `POST /v1/schedules/{schedule_id}/{action}` | yes | pause, resume or remove a schedule. | `brainstem-agent schedules pause <schedule_id>` (also resume and remove) |
| `GET /v1/inbox` | yes | Results of scheduled runs, newest first. | `brainstem-agent inbox --json` |
| `GET /v1/skills` | yes | Skills of the workspace and profile. | `brainstem-agent skills list --json` |
| `GET /v1/skills/{name}` | yes | One skill (?version=N) with its steps and history. | `brainstem-agent skills show <name> --json` (--version picks an older version) |
| `POST /v1/skills/{name}/{action}` | yes | approve ({version}), reject, disable or enable a skill. | `brainstem-agent skills approve <name>` (also reject, disable and enable; --version approves that version) |
| `GET /v1/memory` | yes | Workspace and profile facts (?scope=all, workspace or profile). | `brainstem-agent memory --json` |
| `POST /v1/memory/edit` | yes | Edit a fact ({scope, fact_id, text}). | `brainstem-agent memory edit <fact_id> --scope workspace --text <text>` (--scope profile for a profile fact) |
| `POST /v1/memory/forget` | yes | Forget a fact ({scope, fact_id}). | `brainstem-agent memory forget <fact_id> --scope workspace` (--scope profile for a profile fact) |
| `GET /v1/tools` | yes | The cell's tools: capability, effect, whether a chat turn holds it. | `brainstem-agent api GET /v1/tools` |
| `GET /v1/mcp` | yes | Configured MCP servers and their state. | `brainstem-agent api GET /v1/mcp` (mcp list and mcp status show the same servers) |
| `GET /v1/egress` | yes | The outbound request log (?limit=N, newest last). | `brainstem-agent api GET /v1/egress` (egress log prints the same log) |
| `POST /v1/companion/login` | no | Mint a one-time companion sign-in link (120 s). | `brainstem-agent open` |
| `POST /v1/companion/revoke` | no | End every companion session and pending link. | `brainstem-agent open --sign-out-all` |
| `POST /v1/companion/session` | no | Exchange a one-time token for a session (the login page; no other credential). | `brainstem-agent open` (prints the link whose token the login page exchanges) |
| `GET /v1/companion/session` | yes | Whether this companion session is signed in. | `brainstem-agent api GET /v1/companion/session` |
| `POST /v1/companion/logout` | yes | End this companion session. | `brainstem-agent open --sign-out-all` (ends every companion session, this one included) |

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
- **Limit: reverse DNS.** The cell's own servers (broker, daemon and the companion it serves)
  bind without asking DNS about their address, but the unchanged Grail's web server (werkzeug's, a stdlib
  `HTTPServer`) looks up the name of `127.0.0.1` every time a worker starts. macOS normally
  answers that at once from the `127.0.0.1 localhost` line in `/etc/hosts`; on a Mac whose
  reverse lookup of `127.0.0.1` is slow, every worker start waits for it.

Environment: `BRAINSTEM_AGENT_HOME` (default `~/.brainstem-agent`, owner-only),
`BRAINSTEM_AGENT_CACHE` (default `$BRAINSTEM_AGENT_HOME/cache`),
`BRAINSTEM_AGENT_WORKSPACE`, `BRAINSTEM_AGENT_MODEL` (default `auto`),
`BRAINSTEM_AGENT_GRAIL_SEED` (verified local seed instead of codeload),
`BRAINSTEM_AGENT_GITHUB_TOKEN_FILE` (explicit credential; no fallback),
`BRAINSTEM_AGENT_MIN_FREE_MB` (the disk floor), `BRAINSTEM_AGENT_NO_REDIRECT` (run a command
as installed, not in the home's active version), `$BRAINSTEM_AGENT_HOME/operations.json`
(retention, logs and disk policy, see Operating the cell) and
`$BRAINSTEM_AGENT_HOME/reach.json` (web egress policy and MCP servers, see above),
`BRAINSTEM_HOME` (installed brainstem, default `~/.brainstem`; its
`src/rapp_brainstem/.copilot_token` is read read-only and handed to workers as
`GITHUB_TOKEN`). Credential values, worker keys and grant handles are never
printed, logged, persisted or returned.

## Operating the cell (health, logs, backups, upgrades, removal)

```sh
python3.11 -m brainstem_agent version --json
python3.11 -m brainstem_agent doctor
python3.11 -m brainstem_agent status --json
python3.11 -m brainstem_agent logs --event 'turn.*' --since 2h --json
python3.11 -m brainstem_agent stats --since 7d --json
python3.11 -m brainstem_agent backup --json
python3.11 -m brainstem_agent restore ~/.brainstem-agent/backups/20260923T120000Z --json
python3.11 -m brainstem_agent export --output ~/brainstem-export --json
python3.11 -m brainstem_agent prune --dry-run --json
python3.11 -m brainstem_agent compact --json
python3.11 -m brainstem_agent upgrade --from ../brainstem-agent-new --dry-run --json
python3.11 -m brainstem_agent rollback --json
python3.11 -m brainstem_agent stop --drain --json
python3.11 -m brainstem_agent uninstall --dry-run --json
```

- **One health model** (`health.py`). *Live* means the process is up and its loops run: for
  the daemon, its control server answers and its schedule loop is alive; a cell that is not
  live needs a (re)start. *Ready* means a turn can run now: the pinned Grail source verifies,
  the worker interpreter is prepared, the Copilot credential is usable (present and not known
  to be rejected), the sandbox works, the store opens with a schema this version reads, disk
  space is above the floor, the warm worker is up and the MCP servers are healthy, and the
  daemon is not draining. Every check has an `id`, `kind` (`liveness` or `readiness`), `ok`,
  `required`, a `reason` and, when it fails, a `fix` (the command or action that repairs it).
  `doctor` reports the installation's checks (its `checks` keep their earlier shape, plus
  `reason`, `fix` and `required`; `health` has the summary), `status --json` has the
  daemon's as `readiness` (without a daemon: not live, with the command that starts it), and
  the daemon answers `GET /v1/health` (same bearer token as every route) and `GET
  /v1/version`. `setup`, `restore`, `upgrade`, `rollback` and `compact` end with the same
  summary; the daemon logs every change of readiness (`health.changed`). MCP health and
  `operations.json` problems are advisory for `doctor` (they never block a turn) and required
  for the daemon's readiness (MCP only).
- **Version and release manifest** (`release.py`). `version --json` reports the product
  version and version id (`<version>-<first 12 hex of the tree digest>`), the install kind
  (`pip`, `source` or `home-version`), the Grail pin (repository, commit, version, kernel
  SHA-256, inventory SHA-256 and file count), the store schema (version, SHA-256 of the
  normalized schema, the older layouts it migrates, and the home's store as found,
  read-only), the bridge and worker-lock SHA-256, the capability set, the installed versions
  and whether this installation still matches its release manifest (exit 1 when it does not).
  The manifest, `brainstem_agent/data/release-manifest.json`, is produced from the tree
  (`PYTHONPATH=runtime python3.11 -m brainstem_agent.release --write`; `--check` exits 1 when
  it is stale, and a unit spec enforces it) and lists every shipped file's SHA-256, the Grail
  pin, the store schemas this version reads and migrates, the bridge and lock digests and the
  capabilities.
- **Backup and restore** (`backup.py`). `backup` writes a directory (0700, files 0600;
  default `$BRAINSTEM_AGENT_HOME/backups/<UTC time>`, or `--output DIR`, new or empty): the
  store copied with SQLite's online backup API (a consistent snapshot while the daemon runs,
  integrity-checked; a store another connection keeps locked for 60 s fails the backup, with
  nothing left behind, instead of waiting forever): conversations, memory and profile, skills,
  schedules and the inbox,
  receipts and journals; `state/mcp-pins.json`; `operations.json`; `reach.json` with its
  secrets removed; and `manifest.json` with every file's SHA-256 and size, the store's schema
  and row counts, and what was excluded and why. Excluded by default and reported: every MCP
  server environment value, URL credentials and query strings, and any string shaped like a
  credential (`--include-secrets` keeps `reach.json` as it is). Never included: the Copilot
  credential (the cell never stores it), the daemon's token (`run/`), caches, worker trees,
  logs, the derived search index and workspace files (back up project folders with your usual
  tools). `restore DIR` verifies every digest (and that nothing unlisted is present) before
  it writes anything and refuses on any mismatch; it restores into a new home, or with
  `--replace` over an existing store after a safety backup of it (`backups/pre-restore-*`);
  it refuses while the daemon runs, keeps an existing `reach.json` rather than its
  secret-less copy, lists what must be re-entered, and ends with the health summary. A
  backup of an older store is migrated on restore (with its own pre-migration copy); a backup
  from a newer version (a higher store schema version, even with the same tables) is refused.
  Restoring into a home at another path re-maps the in-home workspaces
  (`workspaces/<name>`): their paths, and the memory namespaces derived from them, are
  rewritten in the restored store (`remapped` in the result), so their memory, skills,
  sessions, schedules and inbox stay visible and schedules run in the new home. Workspaces
  outside the home keep their paths. Missing parents of `--output` are created 0700; an
  existing parent keeps its permissions.
- **Export** writes this workspace's and the profile's knowledge in portable formats:
  `skills/<scope>-<name>.md` (markdown with frontmatter, importable with `skills import`),
  `memory.jsonl` and `profile.jsonl` (one fact per line: `fact_id`, `scope`, `text`,
  `created_at`, `updated_at`, `source_turn`), `sessions/<session_id>.jsonl` (one message per
  line: `turn_id`, `role`, `content`, `state`, `at`) and `export-manifest.json` (formats and
  SHA-256 of every file).
- **Migration safety** (`state.py`). Opening a store looks first, read-only. An older layout
  is copied aside with the online backup API (`state/pre-migration/*.sqlite3`, 0600,
  integrity-checked) before the migration's single transaction runs; if the copy cannot be
  written, nothing is migrated. A migration that is interrupted (even SIGKILL inside the
  transaction, which `BRAINSTEM_AGENT_CRASH_AT=store.migrating` injects) leaves the original
  store intact: SQLite rolls the transaction back on the next open, which migrates it again.
  A store from a newer version (a higher schema version, or this version's tables plus unknown
  ones) is refused with a clear message, without writing a byte.
- **Upgrade and rollback** (`lifecycle.py`). The home keeps runtime versions side by side in
  `versions/<version id>/` and names the active one in `versions/active.json`. `upgrade
  --from PATH` takes a local release only (a checkout, its `runtime/`, a zipapp or a wheel;
  nothing is downloaded): it verifies the release against its manifest, checks that it can
  open this home's store (and has its Grail source and worker interpreter), takes a
  pre-upgrade backup, drains the daemon (`POST /v1/drain`: the running turn and scheduled run
  finish, nothing new starts; `--drain-timeout`, default 300 s, then it gives up unless
  `--force`), keeps the running version installed, switches the active version, points the
  LaunchAgent at it and restarts the daemon (same workspace). A new version that does not
  come up is rolled back automatically: upgrade waits for a daemon answering as the new
  version, also when the LaunchAgent starts it (up to 60 s,
  `BRAINSTEM_AGENT_SERVICE_START_TIMEOUT`), and otherwise switches back, re-points the
  LaunchAgent and restarts the previous version. From then on every command (the console script,
  `python -m brainstem_agent`, the zipapp) continues in the active version, as do the daemon
  and the LaunchAgent; a separately installed newer release is not redirected (it warns), and
  `BRAINSTEM_AGENT_NO_REDIRECT=1` runs a command as installed. `rollback` switches to the
  previous version when it can read the store. When the new version has already migrated the
  store beyond it, rollback refuses and names the pre-upgrade backup; `rollback --restore
  DIR` then puts that store back first (the current store goes to a safety backup, so later
  changes are not lost but are not in the rolled-back store).
- **Uninstall** (`lifecycle.py`). `uninstall --dry-run` lists exactly what `uninstall`
  removes: this home's LaunchAgent plist (booted out first), the daemon (stopped), the cache
  (when inside the home), worker trees, `run/`, logs and installed versions, and what it
  keeps: the store, workspaces, backups and settings, a cache outside the home, the installed
  RAPP Brainstem (never touched) and the runtime package itself (`pip uninstall
  brainstem-agent`, or delete its venv or zipapp). `--remove-home` also removes the whole home,
  only with `--confirm` followed by the home's exact path (exit 2 otherwise), and never when
  the home overlaps the installed brainstem, is or holds your home folder, or holds entries
  Brainstem Agent did not create. `BRAINSTEM_AGENT_LAUNCH_AGENTS` and
  `BRAINSTEM_AGENT_LAUNCHCTL` redirect the LaunchAgents directory and launchctl, as for
  `service`.
- **Credential lifecycle** (`credential_state.py`). When GitHub rejects the sign-in (revoked,
  expired or replaced) or the account has no Copilot access, as unchanged Grail reports at
  worker start or mid-turn, the cell records an explicit state in `state/credential.json`
  (0600): the kind, a reason, when, and the credential file's identity (device, inode, size,
  modification time), never the credential or anything derived from its value. New turns
  (and scheduled runs) are then refused at once with guidance, before anything is written or
  started, and the daemon stops starting workers: no retry storm. Replacing the token in the
  installed brainstem's file (signing in again) changes that identity: the next turn, or the
  daemon's next idle pass, uses the new token without a restart, and success clears the
  state. After a backoff (10 minutes, doubling to 6 hours) one probe is allowed. `doctor`,
  `status` and `/v1/health` show the state with its fix. A replay of a retained turn is never
  refused.
- **Resource hygiene** (`hygiene.py`). `$BRAINSTEM_AGENT_HOME/operations.json` (optional;
  every key defaults) holds the policy:
  `{"retention": {"receipts_days": 90, "run_events_days": 30, "egress_days": 30,
  "inbox_days": 90, "inbox_keep_per_schedule": 20, "pre_migration_days": 30}, "logs":
  {"max_bytes": 1048576, "keep": 3, "worker_logs_keep": 20, "worker_log_max_bytes":
  4194304}, "disk": {"min_free_mb": 512}}` (`BRAINSTEM_AGENT_MIN_FREE_MB` overrides the
  floor; bad values keep their defaults and show as an advisory). Logs are bounded: the event
  log rotates at `max_bytes` keeping `keep` parts, the daemon's output (`logs/daemon.log`) is
  rotated in place (copy, then truncate) by the daemon, a worker's log rotates at 4 MiB, and
  only the newest `worker_logs_keep` worker logs are kept. `prune --dry-run` reports, per
  category, exactly what `prune` then deletes: finished receipts older than the cutoff (never
  those of a turn in progress, nor those a schedule's taint provenance reads), streamed run
  events, finished inbox entries (each schedule keeps its newest ones), egress log entries and
  old pre-migration copies; turns and their replay keys are never pruned. `compact` runs
  SQLite's VACUUM (it needs the home to itself: stop the daemon first, and room for a copy of
  the store). Below the disk floor new turns are refused with a clear message before anything
  is written (`evidence.refused` is `disk`), and `doctor`/`status` show the fix.
- **Errors are JSON.** A store that cannot be opened or a refused workspace is reported as
  `{"ok": false, "error": ...}` with exit 1 under `--json` (one `Brainstem Agent: ...` line
  on stderr otherwise), never as a traceback; `doctor --deep` reports a workspace the guard
  refuses (for example one that contains the home) as its failing deep check.
- **Local observability** (`observe.py`). The event log (`logs/events.jsonl`, 0600) records
  operational events: `turn.started`, `turn.finished` (state, seconds, Grail requests, tool
  calls, error), `turn.refused` (reason), `credential.invalid`, `.changed` and `.recovered`,
  `daemon.started`, `.draining`, `.stopped`, `worker.warm_failed`, `health.changed`,
  `logs.rotated`, `store.migrated`, `backup.created`, `restore.completed`,
  `upgrade.completed`, `rollback.completed` and `prune.completed`; never the owner's words,
  answers or credentials. `logs` reads it (`--source events`, the default) or the daemon's
  output (`daemon`), the workers' logs (`worker`) or the outbound request log (`egress`), with
  `--since`/`--until` (`30m`, `2h`, `7d` or an ISO date-time), `--level`, `--event` (a name or
  pattern), `--turn`, `--grep` and `--limit`. `stats --json` computes from the store, for the
  whole home or one `--workspace`, in a `--since`/`--until` window: turns and outcomes
  (`partial` from the journal), the uncertain count (turns and receipts), latency
  percentiles (nearest rank p50/p90/p95/p99, max and mean), Grail requests, helpers, tool
  usage (calls, states and duration percentiles per tool) and scheduled runs (states, fire
  delay percentiles, late runs, missed instants, skips); refused turns come from the event
  log. Nothing leaves the machine.

## Tests and evidence

The unit tier, from the repository root:

```sh
PYTHONPATH=runtime python3.11 -m unittest discover -s runtime/tests -q
```

The gated tiers also need `runtime/tests` on `PYTHONPATH`. The real-core line starts the real
Grail without inference; the live suites spend real Copilot inference:

```sh
export PYTHONPATH=runtime:runtime/tests
BRAINSTEM_AGENT_REAL_CORE=1 python3.11 -m unittest test_real_core test_real_lifeline test_real_daemon test_real_learning test_real_longturn test_real_reach test_real_operable
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_operable
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_daemon
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_learning
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_longturn
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_reach
BRAINSTEM_AGENT_LIVE=1 python3.11 -m unittest test_live_companion
```

They use about 12 (`test_live`), 10 (`test_live_daemon`), 13 (`test_live_learning`) and 2
(`test_live_operable`) live turns, and about 12 (`test_live_longturn`), 6-10
(`test_live_reach`) and 10 (`test_live_companion`, which needs the browser tooling below)
Grail requests. `test_real_operable` installs the runtime into a fresh venv offline (the
zipapp on 3.12 or newer; on 3.11 it skips, naming the setuptools it found, when that venv
cannot build a wheel offline, as does the unit tier's offline install spec) and runs
`setup` and `doctor --deep` in a fresh home, and starts real daemons for backup, upgrade and
credential specs (the credential spec copies the installed sign-in into a temporary stand-in
brainstem for its duration; the real file is only read).
Setting `BRAINSTEM_AGENT_LIVE_REQUEST_BUDGET` (for example to `12`) caps every live chat's
Grail requests in one test process. The offline retrieval evaluation and the evidence
runner:

```sh
python3.11 runtime/tests/retrieval_eval.py
python3.11 runtime/tests/run_acceptance.py --real-core --live \
  --python-matrix /path/to/python3.13 --matrix-real-core --label NAME --output evidence.json
```

`retrieval_eval.py` takes `--tune` (rerun the grid search) and `--output FILE`.

The companion's browser specs are dev-only and need Node.js 22 or newer: `npm ci` installs
Playwright and axe from `package.json`, and the specs run in Playwright's own headless
Chromium (`npx playwright install chromium` fetches it; `BRAINSTEM_AGENT_CHROMIUM` can name
a headless shell instead). They are separate from the public site's own tests
(`playwright.config.js`). The unit tier runs them through `test_companion_browser` when
Playwright is installed and skips them otherwise; to run them directly:

```sh
npx playwright test -c playwright.companion.config.js hostile.spec.js companion.spec.js
```

`BRAINSTEM_AGENT_TEST_PYTHON` names the interpreter that runs their daemons (default:
`python3.11` on `PATH`), and `BRAINSTEM_AGENT_NPX` names `npx` for the Python bridges when it
is not on `PATH`. `live.spec.js` is the live companion journey that `test_live_companion`
runs.

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
(learning), D1-D12 (long-horizon), E1-E12 (reaching), G1-G12 (companion surfaces) and H1-H12
(operable: install, version and manifest, backup/restore/export, migration safety,
upgrade/rollback, uninstall, health, credential lifecycle, resource hygiene, observability;
`test_cell_operable_*`, including `test_cell_operable_hardening` for the failure classes
found by review, `test_real_operable`, `test_live_operable`). `run_acceptance.py` writes
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
