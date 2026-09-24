# Companion surfaces: threat model and contract

Brainstem Agent gains two owner-facing surfaces that mirror the headless engine: an
interactive terminal (`brainstem-agent`, `repl`) and a local web companion served by the
daemon. The engine is unchanged: every companion action is a documented daemon API route
(`GET /v1/api` lists them) that the CLI can also reach (`brainstem-agent api METHOD PATH`).

This document was written **before** the companion. Every threat names the hostile test
that proves its mitigation (`runtime/tests/test_companion_security.py` = **S**,
`runtime/tests/browser/hostile.spec.js` and `companion.spec.js` = **B** (a real headless
Chromium), `runtime/tests/test_companion_repl.py` = **R**).

## 1. What is protected

| Asset | Why it matters |
|---|---|
| Owner authority: starting turns (tools: files, shell, web, MCP, helpers, schedules) | a turn is arbitrary code-adjacent power inside the workspace |
| Owner data: sessions, answers, receipts, memory, profile, skills, inbox, egress log | private; also the source of stored-XSS payloads |
| Review authority: approving skills, editing/forgetting memory, pausing schedules | an attacker who can approve a quarantined skill can persist instructions |
| Secrets: daemon bearer token, one-time login token, session cookie, CSRF secret | each one grants some of the above |
| The owner's terminal | untrusted text printed to it can drive the terminal emulator |

## 2. Adversaries

1. **Hostile web page** in the owner's browser (any origin, including `http://127.0.0.1:<other
   port>` and `http://localhost:<port>`), able to run JavaScript, submit forms, embed frames,
   open windows, load `<script>`/`<img>` from any URL, use `fetch`/`sendBeacon`/WebSocket,
   and control DNS for its own domain (**DNS rebinding**).
2. **Hostile content inside the cell**: web pages, search results, MCP results, tool output,
   file names, memory, skills, schedule names, model answers. All of it is untrusted text
   that the companion and terminal render.
3. **Other local users and processes** that can connect to loopback ports, list processes
   (`ps` shows argv) and bind their own loopback ports. Processes running **as the owner**
   already hold the owner's files (including `run/daemon.json`); they are out of scope.
4. **Sandboxed cell processes** (Grail workers, shell tools, scripts, MCP servers): their
   Seatbelt profiles deny loopback (except a worker's own broker) and reading the home
   (`contracts/cell.md`, `test_cell_daemon` B9).

## 3. Trust boundaries

```
 browser tab (origin http://127.0.0.1:P) --fetch + cookie + X-Brainstem-CSRF--> daemon :P (127.0.0.1 only)
 other tabs/origins ------ refused: Host, Origin, Sec-Fetch-*, CSRF, content type --X
 CLI / REPL / agents ---- Authorization: Bearer <run/daemon.json token> -------> same route table
 daemon ------------------ unchanged engine (AgentHost, store) --------------------> Grail workers
```

The daemon keeps one listener, `127.0.0.1:<ephemeral port>`. Two credentials reach the same
documented routes: the **bearer token** (CLI, agents; unchanged) and a **companion session**
(browser; an allowlisted subset of routes).

## 4. Threats, mitigations, tests

| # | Threat | Mitigation | Tests |
|---|---|---|---|
| T1 | **DNS rebinding**: `evil.test` re-resolves to 127.0.0.1, so its page talks to the daemon "same-origin" | bind 127.0.0.1 only; exact **Host allowlist** before anything else: `127.0.0.1:<port>` (and `localhost:<port>` for bearer calls only); every other Host (other names, other ports, no port, `[::1]`, `0.0.0.0`, `127.1`, decimal, duplicated or absent Host) gets 403 for pages, assets and API | S `test_t1_*`, B `rebinding` |
| T2 | **CSRF** (forms, `fetch no-cors`, `sendBeacon`, image loads) | every companion request needs the cookie **and** the `X-Brainstem-CSRF` header (a custom header forces a CORS preflight, which the daemon never grants); state changes also need `Origin` exactly `http://127.0.0.1:<port>`, `Content-Type: application/json`, and `Sec-Fetch-Site: same-origin` when sent; SameSite=Strict cookie | S `test_t2_*`, B `csrf` |
| T3 | **Port-shared cookies**: cookies are keyed by host, not port, and every `127.0.0.1` port is *same-site*, so SameSite=Strict does not stop a page on another local port, and a server on another port receives the cookie | the cookie alone never authenticates: the CSRF secret is returned once by the login exchange and kept in `sessionStorage` (origin = port scoped), sent as a header; Origin must match exactly (not "any localhost" as Grail allows) | S `test_t3_*`, B `same-site other port` |
| T4 | **Cross-origin reads** (`fetch` with credentials, `<script src>` JSON hijacking, `<img>`/`<link>`, EventSource, XS-Leaks via frames/windows) | no `Access-Control-Allow-*` ever (preflights get 405); data routes need the CSRF header, `Sec-Fetch-Mode` not `navigate`/`no-cors`; `Cross-Origin-Resource-Policy: same-origin`, `X-Content-Type-Options: nosniff`, JSON objects only, `<`/`>`/`&` escaped in JSON; COOP `same-origin` severs window references | S `test_t4_*`, B `cross-origin reads` |
| T5 | **Login-token leakage** (history, Referer, server logs, `ps`, shared terminals, other users) | `open` asks the daemon (bearer) for a random 256-bit token valid **once** for 120 s; only its SHA-256 is held in daemon memory, never on disk; the URL carries it in the **fragment** (`/login#token`: never sent in a request line, a Referer or a log); the login page removes it from the address bar and history entry before it exchanges it by `POST`; tokens in query strings are ignored; `open` never launches a browser (argv is visible to other users) | S `test_t5_*`, B `login` |
| T6 | **Session theft / fixation** | server-issued session id (256-bit) as `HttpOnly; SameSite=Strict; Path=/` session cookie named per port; a client-chosen cookie is ignored; idle 1 h / absolute 12 h; at most 8 sessions; logout and `open --sign-out-all` revoke server-side; a daemon restart ends every session (memory only). A refused tab is told why (`reason`: `not-signed-in`, `signed-out` for a session this daemon ended, remembered only as a digest, or `daemon-restarted` for one it never started); nothing else is revealed | S `test_t6_*`, `test_g5_a_tab_from_before_a_restart_*` |
| T7 | **Clickjacking** | `Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY` on every response | S `test_t7_*`, B `framing` |
| T8 | **XSS through untrusted content** (answers, deltas, tool output, receipts, MCP status, memory, profile, skills, schedule names/prompts, inbox, egress hosts/paths, file names, errors) | the UI builds DOM with `createElement` + `textContent` only (no `innerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, string timers, `javascript:` URLs; URLs are shown as text, never links); CSP `script-src 'self'; style-src 'self'` (no inline script or style) plus **Trusted Types** (`require-trusted-types-for 'script'; trusted-types 'none'`) so any HTML sink would throw | S `test_t8_*` (static scan), B `hostile payloads` (every field, with CSP and with CSP bypassed) |
| T9 | **Content sniffing** | exact `Content-Type` with charset on every response, `nosniff`; errors are JSON, never HTML; the stdlib default HTML error page is replaced | S `test_t9_*` |
| T10 | **Cache leakage** (shared disk cache, back/forward cache) | `Cache-Control: no-store` on every response; logout sends `Clear-Site-Data: "cache", "cookies", "storage"` | S `test_t10_*` |
| T11 | **Open redirects / path tricks** | the daemon never redirects; login ignores `next`-style parameters; static files come from a fixed name table, never from URL paths (`..`, encoded or absolute paths are 404) | S `test_t11_*` |
| T12 | **Other local processes and users on the port** | everything but static assets needs a credential; tokens compared in constant time; failed login exchanges limited (20 per minute, then 429); the companion session cannot mint login links, call `/v1/tool`, `/chat`, `/v1/turn`, `/v1/stop` or `/v1/wake` (least privilege); request bodies <= 1 MiB; at most 16 streamed turns waiting or running; socket timeout 30 s | S `test_t12_*` |
| T13 | **SSE frame injection** (hostile text containing `\n\ndata:`) | each event is one JSON line; newlines and U+2028/2029 are escaped by the encoder | S `test_t13_*` |
| T14 | **Secrets in logs, evidence, pages** | the daemon never logs requests; no token, cookie or CSRF value appears in `logs/`, the home, API answers (except the one login exchange that returns the CSRF value) or evidence | S `test_t14_*` |
| T15 | **Terminal escape and bidi injection**: answers or tool output containing ESC/OSC sequences (clipboard writes `OSC 52`, hyperlinks `OSC 8`, title changes, screen clears) or bidi controls that reorder what is read (file names, hosts) | the terminal prints untrusted text with C0/C1 control characters (except newline and tab), line separators and every Unicode Bidi_Control character replaced by visible `\x1b` / `\u202e` escapes; the companion shows bidi controls the same way; progress lines are built by the cell | R `test_t15_*`, B `bidi controls` |
| T16 | **History leakage**: REPL input history on disk and in the terminal | `<home>/state/repl_history`, 0600 inside the 0700 home, never through a symlink; a credential-shaped line is kept neither in the file nor in readline's own history (the up arrow), and one found in an old file is never loaded | R `test_t16_*` |
| T17 | **Misleading states** (a streamed answer shown as success when the store says failed; a restart shown as an outage) | streamed text is labelled *streaming, not final*; the final label and answer come from the store; queued, running, streaming, partial, uncertain, failed, cancelled and stale are distinct in text and shape, in the sessions list too; the daemon comes back on its previous port when it is free, so an open tab says "daemon restarted", apart from "unreachable" and "signed out" | S `test_g5_*`, B `honest states`, `daemon restarted` |
| T18 | **Secrets in streamed text**: a credential in an answer shows while it streams (the recorded answer is redacted only at the end), even when split across Grail's fragments | streamed text passes a rolling buffer with the recorded answer's own redaction and every credential shape; text is released only once no secret can still grow across it (a word still arriving, a secret's name waiting for its value, an open quoted value or private-key block) | S `test_t18_*` |

## 5. Residual risks (accepted, documented)

- A process running **as the owner** can read `run/daemon.json` and act as the owner; that is the
  owner's own authority (as for the always-on daemon's CLI token).
- The session cookie cannot carry `Secure` or the `__Host-` prefix over plain-HTTP loopback;
  T3's CSRF secret is what makes a leaked cookie useless.
- A browser extension or a second profile window with access to the tab can act as the owner.
- Other local users can occupy threads with slow unauthenticated connections (local DoS) and see
  that a loopback port is open; they cannot read or change anything.
- The one-time URL stays in the owner's browser history and terminal scrollback; it is dead after
  one use or 120 s.
- A new browser tab needs a new `open` link (the CSRF secret is per tab by design).
- The daemon prefers its previous port (`run/port.json`) so an open tab keeps its origin across
  a restart. If another program listens there, the daemon takes another port and leaves it
  alone; the open tab then says "unreachable" (a new `open` link finds the daemon).
- Streamed text is held back until each word ends (and longer after a secret's name), so text
  appears a fragment later than Grail sends it; the recorded answer is unchanged.
- Grail workers' own loopback ports (pre-existing, Grail's CORS allows any localhost origin) are
  outside this surface; the companion's CSP (`connect-src 'self'`) means the companion page itself
  can never reach them.

## 6. Contract

- `brainstem-agent open` (daemon running; `--json` for agents) prints
  `http://127.0.0.1:<port>/login#<token>` (one use, 120 s). `--sign-out-all` ends every
  companion session. It never opens a browser.
- Login: `GET /login` (static) -> `POST /v1/companion/session {"token"}` -> `Set-Cookie:
  bsa_session_<port>=...; HttpOnly; SameSite=Strict; Path=/` and `{"csrf": ...}` once.
- Companion requests: cookie + `X-Brainstem-CSRF`; `POST` bodies are JSON. A refused sign-in
  is `401 {"error", "reason"}` with `reason` `not-signed-in`, `signed-out` or
  `daemon-restarted`.
- The daemon binds `127.0.0.1` on the port it used last (`run/port.json`) when nothing
  listens there, else on a free ephemeral port.
- Every route (method, path, whether the companion may call it, its CLI equivalent) is in
  `brainstem_agent/api.py` `ROUTES`, served at `GET /v1/api`, and listed in `runtime/README.md`.
- Streaming: `POST /v1/requests` starts a turn (202, `request_id`, state `queued`);
  `GET /v1/requests/<id>/events?after=<seq>` is `text/event-stream` (`id:` = sequence number,
  `data:` = one JSON event: `request.queued`, `request.running`, the engine's progress events,
  `answer.delta` and finally `request.finished` with the turn result); `POST /v1/cancel
  {"request_id"}` cancels. `answer.delta` text is display-only and already redacted (T18);
  `request.finished` carries the recorded answer.
- Security headers on every response: `Content-Security-Policy: default-src 'none'; script-src
  'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action
  'none'; frame-ancestors 'none'; object-src 'none'; require-trusted-types-for 'script';
  trusted-types 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`,
  `Cross-Origin-Resource-Policy: same-origin`, `Cache-Control: no-store`.
