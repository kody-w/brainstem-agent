# RAPP Brainstem

**RAPP Brainstem** is a local-first AI agent engine: you install the Brainstem core on your
computer and sign in with your GitHub account's Copilot access. This repository holds the
public site and **Brainstem Agent**, the always-on agent runtime built around that core.

- **Site:** https://kody-w.github.io/brainstem-agent/
- **Runtime documentation:** [`runtime/README.md`](runtime/README.md)
- **Core installer and core documentation:** https://github.com/kody-w/rapp-installer

## What Brainstem Agent is

Brainstem Agent runs the **unchanged Brainstem Grail core** (the stable core edition) inside
private, sandboxed workers, and adds everything around it: the sandbox, memory, schedules,
tools, helpers, web and MCP. You use it from an interactive terminal session or an
owner-only web companion in your browser; other AI agents drive the same engine through
`--json` commands. The core is pinned to
[`kody-w/rapp-installer`](https://github.com/kody-w/rapp-installer) commit
`49db80c8c6b6caa7647369beaf477d374a8f293c` (version 0.6.16, kernel `brainstem.py` SHA-256
`bd55a7f0bcf5efd3f7966ca39bb146da3c25fda9a0b1ce5ba587919d3c3775f4`). It is never modified
or redistributed: `setup` downloads that public source and checks every file against the
hash inventory in `runtime/brainstem_agent/data/` before each use.

One way to picture it: the core is the cell's mitochondrion, its unchanged power source;
Brainstem Agent is the cell around it (membrane = sandbox, nucleus = state and memory,
organs = tools).

## Status

Experimental, version 0.2.0; interfaces may change. **macOS only**, qualified on Apple
silicon. It is not a hosted service. The Brainstem core installer supports Linux and
Windows; the agent runtime does not yet.

## Requirements

- macOS with `/usr/bin/sandbox-exec` (built into macOS).
- Python 3.11 or newer (verified on 3.11 and 3.13), Git and internet access.
- RAPP Brainstem installed and signed in with a GitHub account that has Copilot access.
  Brainstem Agent uses your Brainstem's GitHub Copilot connection only (by design); it needs
  no separate API key.

## Install

**Step 1, the core** (the existing installer; then run `brainstem`, open
http://localhost:7071 and complete the GitHub sign-in). On macOS or Linux:

```sh
curl -fsSL https://kody-w.github.io/rapp-installer/install.sh | bash
```

On Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/kody-w/rapp-installer/main/install.ps1 | iex
```

**Step 2, the agent** (macOS):

```sh
git clone https://github.com/kody-w/brainstem-agent.git
cd brainstem-agent
python3.11 -m venv .venv
.venv/bin/pip install ./runtime
.venv/bin/brainstem-agent setup
.venv/bin/brainstem-agent doctor --deep
```

`setup` downloads and verifies the pinned core and builds its private environment; `doctor --deep`
proves the sandbox, worker and sign-in are ready.

**First run:**

```sh
.venv/bin/brainstem-agent chat "Create notes/hello.txt containing hi"
.venv/bin/brainstem-agent serve --detach
.venv/bin/brainstem-agent status
```

`serve --detach` starts the always-on daemon (it keeps a warm worker and runs schedules). Files
the agent creates land in its workspace, by default
`~/.brainstem-agent/workspaces/default/` (choose another with `--workspace DIR`). Stop the
daemon with `.venv/bin/brainstem-agent stop`. Every command accepts `--json`, so another AI
agent can drive it. Commands, options and internals are documented in
[`runtime/README.md`](runtime/README.md), including an offline install (pip offline on Python
3.11 needs setuptools 70.1 or newer, or the `wheel` package, in the venv; otherwise the
single-file zipapp, which works with any Python 3.11 or newer) and the operations commands:
`version`, health in `doctor` and `status`, `logs`, `stats`, `backup`, `restore`, `export`,
`prune`, `compact`, `upgrade`, `rollback` and `uninstall` (see "Operating the cell").

**Keep it healthy:**

```sh
.venv/bin/brainstem-agent version
.venv/bin/brainstem-agent status
.venv/bin/brainstem-agent backup
.venv/bin/brainstem-agent restore <backup-directory>
.venv/bin/brainstem-agent upgrade --from <new-checkout> --dry-run
.venv/bin/brainstem-agent rollback
.venv/bin/brainstem-agent uninstall --dry-run
```

`status` says whether the daemon is live (running) and ready (a turn can run now), with a fix
for each failing check. `backup` writes a verified copy of conversations, memory, skills,
schedules and settings to `~/.brainstem-agent/backups/`; `restore` puts one into a fresh home
(or over the current one with `--replace`, after a safety copy). `upgrade` installs a newer
local checkout next to the current version and switches back by itself if it does not start;
`rollback` returns to the previous version. `uninstall --dry-run` lists exactly what
`uninstall` would remove; it never touches the installed RAPP Brainstem.

**Terminal session and web companion:**

```sh
.venv/bin/brainstem-agent
.venv/bin/brainstem-agent open
```

With no command, `brainstem-agent` opens an interactive terminal session: answers stream as
they are written, tools and steps show one line each, slash commands (`/help` lists them)
show sessions, skills, memory, schedules and the inbox, and Ctrl-C cancels the running turn
but keeps the session. With the daemon running, `open` prints a one-time sign-in link for
the web companion, which the daemon serves to this Mac only (127.0.0.1): chat, sessions,
schedules and inbox, skill review, memory and profile, MCP servers and the outbound request
log. Paste the link into your browser; it works once, for two minutes. Both show the same
conversations, and every action in them is also a command.

## Capabilities

Status labels: **Available (experimental)**, **Partial**, **In development**, **Not yet**.

| Capability | Status | What it does | Limit |
| --- | --- | --- | --- |
| Deploy | Partial | `setup` downloads and verifies the pinned core and builds a private environment; `doctor --deep` proves readiness | This Mac only; no cloud or remote deployment yet |
| Always available | Partial | `serve --detach` runs an always-on daemon with a warm worker; `service install` adds a login LaunchAgent | Work pauses while the Mac sleeps; no remote host yet |
| Schedule | Available (experimental) | Ask in plain language or use `schedules`: one-time, interval and cron, time zones and DST, missed-run and overlap rules, pause/resume/edit/run-now/remove, results in an inbox | Runs only while the daemon is running |
| Connect | Not yet | Messaging apps (Telegram, Discord, Slack, email) are not connected yet; today you use the terminal, and tools connect through MCP | No messaging channels |
| Remember | Available (experimental) | Workspace and profile memory with correction and forgetting, search across past conversations, AGENTS.md context files | Local to this Mac; retrieval is keyword ranking (BM25), not semantic embeddings |
| Isolate | Available (experimental) | Every core worker, shell command, script and local MCP server runs under the macOS sandbox (Seatbelt) with private per-worker files; tool processes cannot read your home folder or your sign-in and write only inside the workspace; the shell has no network | macOS sandbox only; not a VM or container |
| Learns skills | Available (experimental) | Saves how it solved a task as a versioned skill (unreviewed until you approve), offers it in later turns, lets you review, approve, edit, disable, export | Skills are data offered to the model, never executed and never granting permissions; review is manual |
| Long tasks | Available (experimental) | Continues automatically past the core's three-tool-round limit, streams progress, Ctrl-C cancels and stops the worker | Bounded per turn (segments, tool calls, time) |
| Helpers | Available (experimental) | Runs up to three helper workers in parallel, each a fresh sandboxed core worker with narrower permissions | One level of helpers; bounded count |
| Tools and processes | Available (experimental) | Files, sandboxed shell, sandboxed Python scripts that call tools, background processes (start, poll, log, stop) | Inside the workspace; shell has no network |
| Web | Available (experimental) | Fetches pages and searches (Wikipedia by default) with citations; private and cloud-metadata addresses are refused; owner egress policy and log; web content can never make it remember or forget | Search provider is Wikipedia unless configured |
| MCP | Available (experimental) | Connects MCP servers (stdio and streamable HTTP) from a JSON config; tools appear as `mcp__server__tool`; each local server gets its own sandbox profile and permission | You add and trust servers yourself |
| Receipts and recovery | Available (experimental) | Every tool call gets a receipt (succeeded, failed or uncertain); repeated requests replay without a new model call; interrupted work is recovered or marked uncertain; `backup` writes a verified snapshot while the daemon runs and `restore` checks every digest before writing; `export` writes skills, memory, profile and sessions in portable formats; `upgrade` installs a local release side by side and switches back by itself if it does not come up, `rollback` returns to the previous version; a rejected sign-in is an explicit state (turns refused, no retries) and signing in again is picked up without a restart | Backups are local, unencrypted and started by you, and leave out workspace files; upgrades only from a local checkout, zipapp or wheel (nothing is downloaded); rolling back across a store migration restores the pre-upgrade backup; a revoked sign-in can keep working until the core's cached Copilot token expires (about 25 minutes) |
| Terminal session and web companion | Available (experimental) | `brainstem-agent` alone opens an interactive terminal session (streamed answers, slash commands, Ctrl-C cancels the turn and keeps the session); `open` prints a one-time link to an owner-only web companion served on 127.0.0.1 (chat, sessions, schedules and inbox, skill review, memory and profile, MCP and the outbound request log) | This Mac only; each sign-in link works once, in one tab; the terminal is a line session, not a full-screen interface |
| Browser, images and voice | Not yet | No browser automation, image understanding or generation, transcription or speech yet | - |

## Privacy: what leaves your Mac

- **Inference** goes through the GitHub Copilot sign-in of your installed RAPP Brainstem,
  which Brainstem Agent reuses read-only. Prompts, conversation context and tool results
  used for inference leave the machine.
- **Web tools** contact the sites you ask about, and the search provider (Wikipedia unless
  you configure another). MCP servers you configure receive the calls you allow them.
- **Setup** downloads the pinned core from GitHub and its Python packages (hash-locked) from
  PyPI.
- Brainstem Agent's own state (conversations, memory, skills, schedules, receipts and logs)
  stays in `~/.brainstem-agent` on your Mac. The sign-in is handed only to the core worker
  and is never printed, logged or saved in that state.
- The web companion is served on 127.0.0.1 only and loads nothing from the internet.
- Backups and exports are written where you choose on this Mac, unencrypted; they never
  contain the sign-in, and MCP secrets are left out unless you ask for them.

## Security model (short version)

- **Unchanged core, verified.** Every tracked core file is hash-checked before a worker
  starts and after it stops; a mismatch discards the worker.
- **Sandboxed workers.** Each core worker runs from its own private copy under the macOS
  sandbox, with private home and temporary folders; it cannot read your home folder or the
  agent's state, and reaches tools only through the agent's local broker.
- **Sandboxed tools.** File tools stay inside the workspace (no absolute paths, `..` or
  symlink escapes). Shell commands, scripts, background processes and local MCP servers run
  under their own sandbox profiles: tool processes cannot read your home folder or your
  sign-in and write only inside the workspace (a local MCP server only in its own folder and
  the paths you allow it); the shell has no network.
- **Least authority per turn.** Each turn gets a short-lived grant with explicit capabilities,
  bound to one worker and revoked when the turn ends; helpers get at most their parent's
  permissions. Learned skills are data and never grant anything.
- **Outside content is untrusted.** Web pages, search results and MCP results are marked as
  data; after reading them, a turn can remember or forget only when your own words ask it to.
  Web requests to private, loopback and cloud-metadata addresses are refused, and every web
  and MCP HTTP request is logged.
- **Owner-only companion.** The web companion answers only on 127.0.0.1 under its exact
  address; a one-time link becomes an HttpOnly cookie plus a secret for that one tab, so
  other pages and other local ports can neither read nor change anything; pages carry a
  strict content security policy and show everything from the agent as plain text. The
  terminal session shows escape sequences and bidi controls from outside text as visible
  characters, and keeps credential-shaped input out of its history.

This is a single-owner tool on one Mac. The macOS sandbox is not a virtual machine or
container. Details and known limits: [`runtime/README.md`](runtime/README.md) and
[`contracts/cell.md`](contracts/cell.md).

## Not supported yet

Messaging channels (Telegram, Discord, Slack, email); cloud, hosted or remote deployment;
the agent runtime on Linux or Windows; browser automation, images and voice; encrypted,
scheduled or off-machine backups. Work pauses while the Mac sleeps. Other model providers are
not supported by design.

## Development

### Site

The site is plain HTML, CSS and JavaScript with no build step. Use Node.js 22 or newer:

```sh
npm ci
npx playwright install chromium
npm test
npm run serve
```

`npm test` runs the server tests, then the Playwright browser suite; `npm run serve` starts a
preview at http://127.0.0.1:4173/brainstem-agent/ (set `PORT` to change it). Local tests can reuse an installed Chrome or Edge (or `PLAYWRIGHT_EXECUTABLE_PATH`); CI
always uses bundled Chromium. See `playwright.config.js`.

### Runtime

From the repository root, with Python 3.11 or newer on macOS, this runs the unit tier:

```sh
PYTHONPATH=runtime python3.11 -m unittest discover -s runtime/tests -q
```

The unit tier needs no network, credential or core download. Tests that copy the pinned
core source skip unless a verified seed is available: set `BRAINSTEM_AGENT_GRAIL_SEED` to a
verified `rapp_brainstem` directory, or run `brainstem-agent setup` first (the cache it
creates in `~/.brainstem-agent` is found automatically). Two further tiers are opt-in. The
real-core tier starts the unchanged core process without inference:

```sh
BRAINSTEM_AGENT_REAL_CORE=1 PYTHONPATH=runtime:runtime/tests python3.11 -m unittest \
  test_real_core test_real_lifeline test_real_daemon test_real_learning test_real_longturn test_real_reach \
  test_real_operable
```

The live tier spends real Copilot requests through your Brainstem's sign-in (the companion's
browser specs also need the dev-only Playwright; see the runtime docs):

```sh
BRAINSTEM_AGENT_LIVE=1 PYTHONPATH=runtime:runtime/tests python3.11 -m unittest test_live
```

`runtime/tests/run_acceptance.py` runs the tiers and writes sanitized evidence; see
[`runtime/README.md`](runtime/README.md#tests-and-evidence) for every live suite and the
evidence runner. CI (`.github/workflows/runtime.yml`) runs the unit tier on macOS with Python
3.11 and 3.13 (seeded with the pinned core source from GitHub), the offline fixture harness,
and an install smoke test of `./runtime` in a fresh virtual environment.

## Repository layout

| Path | What it is |
| --- | --- |
| `index.html`, `assets/` | The public site (the only files GitHub Pages publishes) |
| `tests/`, `playwright.config.js`, `package.json` | Site tests and the local preview server |
| `runtime/` | The Brainstem Agent runtime: Python package `brainstem-agent`, its tests and docs |
| `contracts/` | Technical contracts: `cell.md` (the runtime) and `m0.md` (the offline harness) |
| `.github/workflows/` | `pages.yml` (site tests and deploy) and `runtime.yml` (runtime tests) |

## Publishing the site

GitHub Pages uses the Actions workflow `.github/workflows/pages.yml` (**Settings → Pages →
Source → GitHub Actions**). Each push to `main` runs the site tests, stages only `index.html`
and `assets/` (symbolic links are refused) and deploys them; manual dispatch also works.
Verify the live project URL after a deployment: a successful push alone does not mean the
site is live.

## Content maintenance

Before changing installation, capability or feature copy:

1. Keep the site's capability statuses (its manifest in `assets/capabilities.json`), this
   README's table and the runtime's documented behaviour in agreement. Claim nothing above
   its status, and change a status only with evidence (tests or a recorded run).
2. Verify agent install commands verbatim in a fresh virtual environment from a clean
   checkout. For the core installer, check the upstream README, the actual installer scripts
   and the runtime version file, and check that the referenced scripts respond and still
   match upstream's production path. Never execute installers in site tests.
3. Keep account, platform and remote-inference requirements visible: macOS only for the
   agent, a GitHub Copilot sign-in through your Brainstem, and prompts that leave the
   machine. "Local-first" does not mean inference is offline.
4. Quote recorded runs verbatim (trimming with an ellipsis is fine), label them as recorded
   runs of the experimental runtime with their date, and never present them as live.
5. Check documentation destinations; do not assume upstream files are published at
   equivalent Pages-relative paths.
6. Run the browser suite and inspect desktop and mobile layouts, clipboard failure
   behaviour, the no-JavaScript fallback and keyboard navigation.

The core installers track upstream `main`, not an immutable release; the agent runtime pins
its own core commit (above).

The site's illustrations, favicon and social card are original to this project; keep the
site free of other projects' artwork, logos, fonts and copy.

## License

No license has been added to this repository yet.

## Support

- Brainstem Agent (the runtime) and the site: [this repository's
  issues](https://github.com/kody-w/brainstem-agent/issues).
- The Brainstem core and its installers: [kody-w/rapp-installer
  issues](https://github.com/kody-w/rapp-installer/issues).

Neither is a Microsoft support channel.
