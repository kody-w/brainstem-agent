// Brainstem Agent companion: a passive mirror of the daemon's documented API.
// Security rules (contracts/companion.md): every string from the cell is untrusted and is
// only ever placed with textContent / text nodes; nodes come from a tag allowlist; no
// links, images or HTML parsing; every request goes through ROUTES with this tab's CSRF
// secret (sessionStorage, origin-scoped) and the HttpOnly session cookie.

const ROUTES = {
  whoami: ['GET', '/v1/companion/session'],
  logout: ['POST', '/v1/companion/logout'],
  status: ['GET', '/v1/status'],
  health: ['GET', '/v1/health'],
  start: ['POST', '/v1/requests'],
  request: ['GET', '/v1/requests/{request_id}'],
  events: ['GET', '/v1/requests/{request_id}/events'],
  cancel: ['POST', '/v1/cancel'],
  sessions: ['GET', '/v1/sessions'],
  session: ['GET', '/v1/sessions/{session_id}'],
  schedules: ['GET', '/v1/schedules'],
  scheduleChange: ['POST', '/v1/schedules/{schedule_id}/{action}'],
  inbox: ['GET', '/v1/inbox'],
  skills: ['GET', '/v1/skills'],
  skill: ['GET', '/v1/skills/{name}'],
  skillReview: ['POST', '/v1/skills/{name}/{action}'],
  memory: ['GET', '/v1/memory'],
  memoryEdit: ['POST', '/v1/memory/edit'],
  memoryForget: ['POST', '/v1/memory/forget'],
  tools: ['GET', '/v1/tools'],
  mcp: ['GET', '/v1/mcp'],
  egress: ['GET', '/v1/egress'],
};

const TAGS = new Set(['div', 'span', 'p', 'ul', 'ol', 'li', 'h3', 'h4', 'button', 'dl', 'dt',
  'dd', 'pre', 'code', 'strong', 'em', 'small', 'time', 'label', 'textarea', 'details',
  'summary', 'br']);
const ATTRS = new Set(['type', 'id', 'for', 'rows', 'disabled', 'hidden', 'role', 'tabindex',
  'title', 'datetime', 'autocomplete', 'spellcheck', 'open']);
const LABELS = {
  queued: 'queued', running: 'running', streaming: 'streaming (not final)',
  succeeded: 'succeeded', partial: 'partial', uncertain: 'uncertain', failed: 'failed',
  cancelled: 'cancelled', stale: 'stale', reserved: 'queued',
};

// Unicode's Bidi_Control characters are shown as their escape (\u202e), never applied:
// in untrusted text they could reorder what the owner reads (file names, hosts, answers).
const BIDI = /[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;
function inert(text) {
  return String(text).replace(BIDI, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

function h(tag, props = {}, ...children) {
  if (!TAGS.has(tag)) throw new Error(`tag not allowed: ${tag}`);
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = String(value);
    else if (key === 'text') node.textContent = inert(value);
    else if (key === 'on') {
      for (const [name, listener] of Object.entries(value)) node.addEventListener(name, listener);
    } else if (ATTRS.has(key) || key.startsWith('aria-') || key.startsWith('data-')) {
      node.setAttribute(key, value === true ? '' : String(value));
    } else throw new Error(`attribute not allowed: ${key}`);
  }
  for (const child of children.flat(3)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(inert(child)));
  }
  return node;
}

const $ = (id) => document.getElementById(id);
const csrf = sessionStorage.getItem('bsa.csrf');
const state = {
  session: null, live: null, view: 'chat', reachable: true, signedOut: false,
  notes: new Map(), shownSkill: null, editing: null, confirming: null,
};
window.__bsa = { deltaLags: [], firstRender: null, states: [] };

class ApiError extends Error {
  constructor(status, message, reason) { super(message); this.status = status; this.reason = reason; }
}

function url(route, params = {}, query = {}) {
  const path = route[1].replace(/\{(\w+)\}/g, (_m, key) => encodeURIComponent(String(params[key])));
  const search = new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined && v !== null)).toString();
  return search ? `${path}?${search}` : path;
}

async function api(name, { params, query, body } = {}) {
  const route = ROUTES[name];
  const init = {
    method: route[0], credentials: 'same-origin', cache: 'no-store',
    headers: { 'X-Brainstem-CSRF': csrf || '' },
  };
  if (route[0] !== 'GET') {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body || {});
  }
  let response;
  try {
    response = await fetch(url(route, params, query), init);
  } catch {
    connection.unreachable();
    throw new ApiError(0, 'The daemon is unreachable.');
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    connection.signedOut(data.error, data.reason);
    throw new ApiError(401, data.error || 'Signed out.', data.reason);
  }
  connection.reachable();
  if (!response.ok) throw new ApiError(response.status, data.error || `HTTP ${response.status}`);
  return data;
}

// -- connection -----------------------------------------------------------------------
const connection = {
  reachable() {
    if (state.signedOut) return;
    if (!state.reachable) {
      state.reachable = true;
      document.querySelectorAll('[data-stale]').forEach((node) => node.setAttribute('data-stale', 'false'));
    }
  },
  unreachable() {
    if (state.reachable) {
      state.reachable = false;
      document.querySelectorAll('.view').forEach((node) => node.setAttribute('data-stale', 'true'));
    }
    // The daemon comes back on its previous port when it can; then this tab hears
    // "restarted" (signedOut) instead. Unreachable means nothing answers at this address.
    connectionLine('unreachable', 'Daemon unreachable: it stopped, or it restarted on another port. ' +
      'What is shown may be stale; run brainstem-agent open for a new link once it runs again.');
    if (state.live && !state.live.finished) state.live.markStale();
  },
  signedOut(reason, why) {
    state.signedOut = true;
    const restarted = why === 'daemon-restarted';
    $('signed-out-title').textContent = restarted ? 'Daemon restarted' : 'Signed out';
    $('signed-out-reason').textContent = inert(reason || 'This tab is not signed in.');
    $('signed-out').hidden = false;
    document.querySelectorAll('.view').forEach((node) => { node.hidden = true; });
    if (restarted) connectionLine('restarted', 'Daemon restarted: its companion sessions ended, this tab\'s too.');
    else connectionLine('signed-out', 'Signed out');
    $('send').disabled = true;
  },
  describe(status) {
    const worker = (status.workers || [])[0];
    const readiness = status.readiness ? `, ${verdict(status.readiness)[1]}` : '';
    connectionLine('connected', `Connected: daemon ${status.health}${readiness}, worker ${worker ? worker.state : 'not started'}` +
      (status.active_turn ? `, a turn is ${status.active_turn.phase}` : ''));
  },
};

// The connection line: its text, and a state for styles and specs (connecting, connected,
// unreachable, restarted, signed-out).
function connectionLine(name, text) {
  const line = $('connection');
  line.textContent = inert(text);
  line.className = name === 'connected' ? 'connection' : `connection down ${name}`;
  line.setAttribute('data-state', name);
}

async function poll() {
  if (state.signedOut) return;
  try {
    const status = await api('status');
    connection.describe(status);
    if (state.view === 'status') renderStatus(status, await api('health'));
  } catch { /* shown by api() */ }
}

// -- shared bits ------------------------------------------------------------------------
function badge(name, text) {
  const key = /^[a-z-]{1,24}$/.test(String(name)) ? name : 'unknown';
  return h('span', { class: `state state-${key}`, text: text || LABELS[name] || String(name) });
}

function when(seconds) {
  if (typeof seconds !== 'number') return '';
  const date = new Date(seconds * 1000);
  return h('time', { datetime: date.toISOString(), text: date.toLocaleString() });
}

function short(id) { return String(id || '').replace(/^(session|turn|req|occ)_/, '').slice(0, 10); }

function replace(id, ...nodes) {
  const target = $(id);
  target.replaceChildren(...nodes.flat());
  target.setAttribute('data-stale', state.reachable ? 'false' : 'true');
}

function problem(error) {
  return h('p', { class: 'error', role: 'alert', text: error.message || String(error) });
}

function json(value) { return JSON.stringify(value, null, 1); }

function announce(text) { $('announcer').textContent = inert(text); }

function progressLine(event) {
  const who = event.child ? `helper ${event.index ?? ''} ` : '';
  switch (event.event) {
    case 'segment.started':
      return `${who}step ${event.segment} started${event.continuation ? ' (continuing: Grail ran out of tool rounds)' : ''}`;
    case 'segment.finished':
      return `${who}step ${event.segment} ${event.state}: ${event.rounds} tool round(s), ${event.tool_calls} call(s), ${Number(event.seconds).toFixed(1)}s`;
    case 'tool.finished':
      return `${who}${event.inner_of ? '(script) ' : ''}${event.tool} ${event.denied ? 'refused' : event.ok ? 'ok' : 'failed'}`;
    case 'child.started': return `helper ${event.index} started: ${String(event.task || event.name || '').slice(0, 80)}`;
    case 'child.finished': return `helper ${event.index} ${event.state} in ${Number(event.seconds).toFixed(1)}s`;
    case 'limit.reached': return `${who}limit reached: ${event.limit}`;
    case 'turn.continuing': return event.reason === 'unreceipted-tool-log'
      ? `${who}answer not accepted: it wrote tool calls out with no receipt; continuing`
      : `continuing with step ${event.segment}`;
    default: return null;
  }
}

function receiptList(receipts) {
  if (!receipts || !receipts.length) return null;
  return h('details', {}, h('summary', { text: `Receipts (${receipts.length})` }),
    h('ul', { class: 'progress' }, receipts.map((receipt) => h('li', {},
      h('code', { text: receipt.tool }), ' ', badge(receipt.state, receipt.state),
      receipt.parent_turn ? ' (helper)' : '', ' ',
      h('span', { class: 'meta', text: json(receipt.request).slice(0, 300) }),
      receipt.result ? h('pre', { text: json(receipt.result).slice(0, 1200) }) : null))));
}

function journalSummary(turn) {
  const parts = [];
  if (turn.segments && turn.segments.length) parts.push(`${turn.segments.length} step(s)`);
  if (turn.helpers && turn.helpers.length) parts.push(`${turn.helpers.length} helper(s)`);
  if (!parts.length) return null;
  return h('details', {}, h('summary', { text: `Journal: ${parts.join(', ')}` }),
    h('ul', { class: 'progress' },
      (turn.segments || []).map((step) => h('li', {}, `step ${step.seq} `, badge(step.state, step.state),
        ` ${(step.result && step.result.rounds) ?? '?'} round(s), ${(step.result && step.result.tool_calls) ?? '?'} call(s)`)),
      (turn.helpers || []).map((step) => h('li', {}, `helper ${step.seq} `, badge(step.state, step.state),
        ` ${String((step.detail && step.detail.task) || '').slice(0, 120)}`))));
}

// -- chat --------------------------------------------------------------------------------
function turnItem(turn) {
  const note = state.notes.get(turn.turn_id);
  const label = turn.label || turn.state;
  const answer = label === 'succeeded' && typeof turn.response === 'string'
    ? h('p', { class: 'text', text: turn.response })
    : h('p', { class: 'text provisional', text: label === 'stale'
      ? 'Recorded as running, but no turn is running now: its outcome will be settled when the daemon next starts.'
      : (turn.partial && turn.partial.text) || (note && note.error) || 'No answer was recorded for this turn.' });
  return h('li', { 'data-turn': turn.turn_id },
    h('div', { class: 'row' }, badge(label), h('span', { class: 'meta' }, when(turn.started_at))),
    h('p', { class: 'who', text: 'You' }), h('p', { class: 'text', text: turn.user_input }),
    h('p', { class: 'who', text: 'Brainstem Agent' }), answer,
    note && note.streamed && label !== 'succeeded'
      ? h('details', {}, h('summary', { text: 'Streamed text (not recorded as an answer)' }),
        h('p', { class: 'text provisional', text: note.streamed })) : null,
    journalSummary(turn), receiptList(turn.receipts));
}

async function loadSession(sessionId) {
  state.session = sessionId;
  $('session-line').textContent = sessionId ? `Session ${sessionId}` : 'New session';
  if (!sessionId) { replace('transcript'); return; }
  try {
    const view = await api('session', { params: { session_id: sessionId } });
    replace('transcript', view.turns.map(turnItem));
  } catch (error) {
    replace('transcript', h('li', {}, problem(error)));
  }
}

class LiveTurn {
  constructor(message) {
    this.message = message;
    this.requestId = null;
    this.seq = 0;
    this.text = '';
    this.finished = false;
    this.announced = 0;
    this.stateBadge = badge('queued');
    window.__bsa.states.push([null, 'queued']);
    this.answer = h('p', { class: 'text provisional', text: '' });
    this.progress = h('ul', { class: 'progress' });
    this.note = h('p', { class: 'meta', text: 'Waiting for the worker.' });
    this.node = h('li', { 'data-live': 'true', 'aria-busy': 'true' },
      h('div', { class: 'row' }, this.stateBadge), h('p', { class: 'who', text: 'You' }),
      h('p', { class: 'text', text: message }), h('p', { class: 'who', text: 'Brainstem Agent' }),
      this.answer, this.note, this.progress);
    $('transcript').append(this.node);
  }

  setState(name, text) {
    window.__bsa.states.push([this.requestId, name]);
    const next = badge(name, text);
    this.stateBadge.replaceWith(next);
    this.stateBadge = next;
  }

  markStale() {
    this.setState('stale', 'stale (connection lost; state unknown)');
    this.note.textContent = 'The connection to the daemon was lost; this turn may still be running.';
  }

  handle(event) {
    this.seq = Math.max(this.seq, event.seq || 0);
    switch (event.event) {
      case 'request.queued': this.setState('queued'); break;
      case 'request.running':
        this.setState('running');
        this.note.textContent = 'Running.';
        break;
      case 'turn.started':
        if (event.session_id && event.session_id !== state.session) {
          state.session = event.session_id;
          $('session-line').textContent = `Session ${event.session_id}`;
        }
        break;
      case 'answer.delta': {
        if (!this.text) {
          this.setState('streaming');
          this.note.textContent = 'Streaming: the answer is not final until the turn finishes.';
          announce('Answer streaming.');
        }
        this.text += event.text;
        this.answer.textContent = inert(this.text);
        if (typeof event.t === 'number') window.__bsa.deltaLags.push((performance.timeOrigin + performance.now()) / 1000 - event.t);
        if (this.text.length - this.announced > 160) {
          announce(this.text.slice(this.announced));
          this.announced = this.text.length;
        }
        break;
      }
      case 'request.finished': this.finish(event.result || {}); break;
      default: {
        const line = progressLine(event);
        if (line) this.progress.append(h('li', { text: line }));
        if (event.event === 'segment.started' && event.segment > 1 && this.text) {
          this.text += '\n';
        }
      }
    }
  }

  finish(result) {
    this.finished = true;
    this.node.setAttribute('aria-busy', 'false');
    const name = result.state || 'failed';
    this.setState(name);
    if (result.turn_id) {
      state.notes.set(result.turn_id, { streamed: this.text, error: result.error });
    }
    if (name === 'succeeded' && result.response) {
      this.answer.textContent = inert(result.response.response);
      this.answer.className = 'text';
      this.note.textContent = '';
    } else {
      this.answer.textContent = inert(result.error || 'The turn ended without an answer.');
      this.note.textContent = this.text ? 'Streamed text was not recorded as an answer.' : '';
    }
    announce(`Answer finished: ${LABELS[name] || name}.${name === 'succeeded' && result.response ? ` ${result.response.response.slice(0, 400)}` : ''}`);
  }
}

async function follow(live) {
  for (let attempt = 0; attempt < 30 && !live.finished; attempt += 1) {
    let response;
    try {
      response = await fetch(url(ROUTES.events, { request_id: live.requestId }, { after: live.seq }), {
        credentials: 'same-origin', cache: 'no-store',
        headers: { 'X-Brainstem-CSRF': csrf || '', Accept: 'text/event-stream' },
      });
    } catch {
      connection.unreachable();
      await new Promise((done) => setTimeout(done, 2000));
      continue;
    }
    if (response.status === 401) {
      const data = await response.json().catch(() => ({}));
      connection.signedOut(data.error, data.reason);
      return;
    }
    if (response.status === 404) { live.markStale(); return; }
    if (!response.ok || !response.body) { live.markStale(); return; }
    connection.reachable();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index = buffer.indexOf('\n\n');
        while (index >= 0) {
          const frame = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          const data = frame.split('\n').filter((line) => line.startsWith('data: '))
            .map((line) => line.slice(6)).join('\n');
          if (data) live.handle(JSON.parse(data));
          index = buffer.indexOf('\n\n');
        }
      }
    } catch {
      connection.unreachable();
    }
    if (!live.finished) await new Promise((done) => setTimeout(done, 1000));
  }
}

async function send(message) {
  const live = new LiveTurn(message);
  state.live = live;
  $('send').disabled = true;
  $('stop').disabled = false;
  try {
    const started = await api('start', { body: { message, session_id: state.session } });
    live.requestId = started.request_id;
    await follow(live);
  } catch (error) {
    live.finish({ state: 'failed', error: `Not sent: ${error.message}` });
  } finally {
    $('send').disabled = state.signedOut;
    $('stop').disabled = true;
    state.live = null;
  }
  if (live.finished && state.session) await loadSession(state.session);
}

// -- views -------------------------------------------------------------------------------
async function renderSessions() {
  try {
    const { sessions } = await api('sessions');
    replace('sessions-body', sessions.length ? h('ul', { class: 'cards' }, sessions.map((item) => h('li', {},
      h('div', { class: 'row' }, badge(item.last_label),
        h('span', { class: 'meta' }, `${item.turns} turn(s) · `, when(item.last_at))),
      h('p', { class: 'text', text: item.last_input }),
      h('div', { class: 'row' }, h('button', {
        type: 'button', 'aria-label': `Resume session ${short(item.session_id)}`,
        on: { click: async () => { await loadSession(item.session_id); show('chat'); } },
      }, 'Resume'), h('code', { text: item.session_id }))))) : h('p', { text: 'No sessions yet.' }));
  } catch (error) { replace('sessions-body', problem(error)); }
}

function focusKey(key) {
  const node = document.querySelector(`[data-key="${CSS.escape(key)}"]`);
  if (node) node.focus();
}

function confirmButton(key, label, action) {
  const armed = state.confirming === key;
  return h('button', {
    type: 'button', class: armed ? 'danger' : 'quiet', 'data-key': key,
    on: { click: async () => {
      if (!armed) { state.confirming = key; await rerender(); focusKey(key); return; }
      state.confirming = null;
      await action();
    } },
  }, armed ? `Confirm: ${label}` : label);
}

async function act(work) {
  const box = $('action-message');
  try {
    await work();
    box.hidden = true;
    box.textContent = '';
  } catch (error) {
    box.textContent = inert(error.message);
    box.hidden = false;
  }
  await rerender();
}

async function renderSchedules() {
  try {
    const [{ schedules }, { inbox }] = await Promise.all([api('schedules'), api('inbox')]);
    replace('schedules-body', schedules.length ? h('ul', { class: 'cards' }, schedules.map((item) => h('li', {},
      h('div', { class: 'row' }, badge(item.state, item.state), h('strong', { text: item.name })),
      h('dl', { class: 'facts' }, h('dt', { text: 'When' }), h('dd', { text: item.when }),
        h('dt', { text: 'Next run' }), h('dd', { text: item.next_fire_local || 'none' }),
        h('dt', { text: 'Written by' }), h('dd', { text: item.created_by }),
        h('dt', { text: 'Prompt' }), h('dd', { class: 'text', text: item.prompt })),
      h('div', { class: 'row' },
        item.state === 'active' ? h('button', { type: 'button', class: 'quiet', on: { click: () => act(() => api('scheduleChange', { params: { schedule_id: item.schedule_id, action: 'pause' } })) } }, `Pause ${item.name}`.slice(0, 60)) : null,
        item.state === 'paused' ? h('button', { type: 'button', class: 'quiet', on: { click: () => act(() => api('scheduleChange', { params: { schedule_id: item.schedule_id, action: 'resume' } })) } }, `Resume ${item.name}`.slice(0, 60)) : null,
        item.state !== 'removed' ? confirmButton(`remove:${item.schedule_id}`, 'Remove schedule', () => act(() => api('scheduleChange', { params: { schedule_id: item.schedule_id, action: 'remove' } }))) : null)))) : h('p', { text: 'No schedules.' }));
    replace('inbox-body', inbox.length ? h('ul', { class: 'cards' }, inbox.map((run) => h('li', {},
      h('div', { class: 'row' }, badge(run.state === 'running' ? 'running' : run.state, run.state), h('strong', { text: run.name || run.schedule_id }),
        h('span', { class: 'meta' }, when(run.scheduled_at || run.finished_at))),
      run.late ? h('p', { class: 'stale-note', text: `Ran late; ${run.missed_count || 0} earlier run(s) missed.` }) : null,
      h('p', { class: 'text', text: (run.result && (run.result.response || run.result.error)) || 'No result recorded.' })))) : h('p', { text: 'The inbox is empty.' }));
  } catch (error) { replace('schedules-body', problem(error)); }
}

async function renderSkills() {
  try {
    const { skills } = await api('skills');
    replace('skills-body', skills.length ? h('ul', { class: 'cards' }, skills.map((skill) => {
      const review = [];
      if (skill.review === 'unreviewed' || skill.review === 'quarantined') {
        review.push(h('button', { type: 'button', on: { click: () => act(() => api('skillReview', { params: { name: skill.name, action: 'approve' }, body: { version: skill.version } })) } }, `Approve ${skill.name} v${skill.version}`));
      }
      if (skill.pending) {
        review.push(h('button', { type: 'button', on: { click: () => act(() => api('skillReview', { params: { name: skill.name, action: 'approve' }, body: { version: skill.pending } })) } }, `Approve pending v${skill.pending}`));
        review.push(h('button', { type: 'button', class: 'quiet', on: { click: () => act(() => api('skillReview', { params: { name: skill.name, action: 'reject' } })) } }, `Reject pending v${skill.pending}`));
      }
      review.push(h('button', { type: 'button', class: 'quiet', on: { click: () => act(() => api('skillReview', { params: { name: skill.name, action: skill.state === 'disabled' ? 'enable' : 'disable' } })) } },
        `${skill.state === 'disabled' ? 'Enable' : 'Disable'} ${skill.name}`));
      return h('li', {},
        h('div', { class: 'row' }, h('strong', { text: skill.name }), badge(skill.review, skill.review), badge(skill.state, skill.state),
          h('span', { class: 'meta', text: `${skill.scope}, v${skill.version}${skill.pending ? `, pending v${skill.pending}` : ''}, by ${skill.provenance.author}${skill.provenance.tainted ? ' (after outside text)' : ''}` })),
        h('p', { class: 'text', text: skill.description }),
        h('div', { class: 'row' }, h('button', { type: 'button', class: 'quiet', on: { click: () => { state.shownSkill = skill.name; renderSkill(); } } }, `Show ${skill.name}`), review));
    })) : h('p', { text: 'No skills yet.' }));
    if (state.shownSkill) await renderSkill();
  } catch (error) { replace('skills-body', problem(error)); }
}

async function renderSkill() {
  try {
    const { skill, history } = await api('skill', { params: { name: state.shownSkill } });
    replace('skill-detail', h('h3', { text: `Skill ${skill.name} (version ${skill.shown_version})` }),
      h('dl', { class: 'facts' }, h('dt', { text: 'Description' }), h('dd', { class: 'text', text: skill.shown_description }),
        h('dt', { text: 'When to use' }), h('dd', { class: 'text', text: skill.shown_when_to_use })),
      h('h4', { text: 'Steps' }), h('ol', {}, (skill.steps || []).map((step) => h('li', { class: 'text', text: step }))),
      h('h4', { text: 'History' }), h('ul', { class: 'progress' }, history.map((item) => h('li', {},
        `v${item.version} `, badge(item.review, item.review), ` by ${item.author}${item.tainted ? ' (tainted)' : ''}: ${item.note || ''}`))));
  } catch (error) { replace('skill-detail', problem(error)); }
}

function factList(facts, scope) {
  if (!facts.length) return h('p', { text: scope === 'profile' ? 'No profile facts.' : 'No facts in this workspace.' });
  return h('ul', { class: 'cards' }, facts.map((fact) => {
    const key = `${scope}:${fact.fact_id}`;
    if (state.editing === key) {
      const field = h('textarea', { id: `edit-${fact.fact_id}`, rows: 3 });
      field.value = fact.text;
      return h('li', {}, h('label', { for: `edit-${fact.fact_id}`, text: `Edit fact ${fact.fact_id}` }), field,
        h('div', { class: 'row' },
          h('button', { type: 'button', on: { click: () => { state.editing = null; act(() => api('memoryEdit', { body: { scope, fact_id: fact.fact_id, text: field.value } })); } } }, 'Save'),
          h('button', { type: 'button', class: 'quiet', on: { click: () => { state.editing = null; rerender(); } } }, 'Cancel')));
    }
    return h('li', {}, h('p', { class: 'text', text: fact.text }),
      h('p', { class: 'meta' }, `${fact.fact_id} · `, when(fact.updated_at || fact.created_at)),
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'quiet', 'aria-label': `Edit fact ${fact.fact_id}`, on: { click: async () => { state.editing = key; await rerender(); const field = $(`edit-${fact.fact_id}`); if (field) field.focus(); } } }, 'Edit'),
        confirmButton(`forget:${key}`, `Forget fact ${fact.fact_id}`, () => act(() => api('memoryForget', { body: { scope, fact_id: fact.fact_id } })))));
  }));
}

async function renderMemory() {
  try {
    const { facts } = await api('memory', { query: { scope: 'all' } });
    replace('memory-workspace', factList(facts.filter((fact) => fact.scope === 'workspace'), 'workspace'));
    replace('memory-profile', factList(facts.filter((fact) => fact.scope === 'profile'), 'profile'));
  } catch (error) { replace('memory-workspace', problem(error)); }
}

async function renderTools() {
  try {
    const [mcp, tools] = await Promise.all([api('mcp'), api('tools')]);
    replace('mcp-body', mcp.servers.length ? h('ul', { class: 'cards' }, mcp.servers.map((server) => h('li', {},
      h('div', { class: 'row' }, h('strong', { text: server.server }), badge(server.state, server.state),
        h('span', { class: 'meta', text: `${server.transport}, capability ${server.capability}, ${server.starts} start(s), ${server.failures} failure(s)` })),
      server.error ? h('p', { class: 'error', text: server.error }) : null,
      h('p', { class: 'meta', text: server.tools.length ? `Tools: ${server.tools.join(', ')}` : 'No tools listed yet.' }),
      Object.keys(server.withheld || {}).length ? h('div', { class: 'stale-note' },
        h('p', { text: `Withheld until you trust this server (brainstem-agent mcp trust ${server.server}):` }),
        h('ul', {}, Object.entries(server.withheld).map(([tool, why]) => h('li', {}, h('code', { text: tool }), `: ${why}`)))) : null))) : h('p', { text: 'No MCP servers are configured (reach.json).' }),
    mcp.problems.map((text) => h('p', { class: 'error', text })));
    replace('tools-body', h('ul', { class: 'cards' }, tools.tools.map((tool) => h('li', {},
      h('div', { class: 'row' }, h('code', { text: tool.name }), h('span', { class: 'meta', text: `${tool.capability}, ${tool.effect}${tool.chat_turn ? ', held by chat turns' : ''}` })),
      h('p', { class: 'text', text: tool.description })))));
  } catch (error) { replace('mcp-body', problem(error)); }
}

async function renderEgress() {
  try {
    const { entries } = await api('egress', { query: { limit: 100 } });
    replace('egress-body', entries.length ? h('ul', { class: 'cards' }, entries.slice().reverse().map((entry) => h('li', {},
      h('div', { class: 'row' }, h('strong', { text: `${entry.method || ''} ${entry.host || ''}` }), h('span', { class: 'meta', text: `status ${entry.status ?? 'none'}` })),
      h('p', { class: 'text', text: entry.path || '' }),
      h('p', { class: 'meta', text: `${entry.tool || ''} · ${entry.ip || ''}:${entry.port || ''} · ${entry.bytes ?? 0} bytes · ${entry.seconds ?? ''}s · ${entry.at || ''}` })))) : h('p', { text: 'No outbound requests yet.' }));
  } catch (error) { replace('egress-body', problem(error)); }
}

// Live (the daemon answers, its loops run) versus ready (a turn can run now), from GET /v1/health.
function verdict(report) {
  if (report.ready) return ['succeeded', 'ready'];
  return report.live ? ['partial', 'live, not ready'] : ['failed', 'not live'];
}

function renderReadiness(report) {
  const [name, text] = verdict(report);
  return [h('h3', { text: 'Readiness' }),
    h('p', { id: 'readiness', 'data-live': String(report.live), 'data-ready': String(report.ready) },
      badge(name, text), ` Live: ${report.live ? 'yes' : 'no'}. Ready: ${report.ready ? 'yes' : 'no'}.`),
    h('ul', { class: 'checks', 'aria-label': 'Health checks' }, report.checks.map((check) => h('li', {},
      badge(check.ok ? 'succeeded' : check.required ? 'failed' : 'partial', check.ok ? 'ok' : check.required ? 'failing' : 'advisory'),
      h('strong', { text: ` ${check.id}` }), h('span', { class: 'meta', text: ` (${check.kind})` }),
      h('p', { class: 'text', text: check.reason }),
      !check.ok && check.fix ? h('p', { class: 'meta', text: `Fix: ${check.fix}` }) : null)))];
}

function renderStatus(status, report) {
  const worker = (status.workers || [])[0];
  replace('status-body', report ? renderReadiness(report) : [], h('dl', { class: 'facts' },
    h('dt', { text: 'Health' }), h('dd', { text: status.health }),
    h('dt', { text: 'Daemon' }), h('dd', { text: `pid ${status.pid}, up ${Math.round(status.uptime_seconds)} s` }),
    h('dt', { text: 'Worker' }), h('dd', { text: worker ? `${worker.state} (${worker.worker_id})` : 'none yet' }),
    h('dt', { text: 'Active turn' }), h('dd', { text: status.active_turn ? `${status.active_turn.turn_id} (${status.active_turn.phase})` : 'none' }),
    h('dt', { text: 'Schedules' }), h('dd', { text: json(status.scheduler.schedules) }),
    h('dt', { text: 'Next fire' }), h('dd', { text: status.scheduler.next_fire_local || 'none' }),
    h('dt', { text: 'Companion' }), h('dd', { text: `${status.companion.sessions} session(s); refused ${json(status.companion.refused)}` })),
  h('h3', { text: 'Last errors' }),
  status.last_errors.length ? h('ul', { class: 'progress' }, status.last_errors.map((item) => h('li', { text: `${item.source}: ${item.error}` }))) : h('p', { text: 'None.' }),
  h('div', { class: 'row' }, h('button', { type: 'button', class: 'quiet', on: { click: () => act(() => api('cancel', { body: { active: true } })) } }, 'Cancel the active turn')));
}

const RENDER = {
  chat: () => (state.live ? null : loadSession(state.session)),
  sessions: renderSessions,
  schedules: renderSchedules,
  skills: renderSkills,
  memory: renderMemory,
  tools: renderTools,
  egress: renderEgress,
  status: async () => {
    try { renderStatus(await api('status'), await api('health')); } catch (error) { replace('status-body', problem(error)); }
  },
};

async function rerender() { if (!state.signedOut) await RENDER[state.view](); }

function show(view) {
  state.view = view;
  document.querySelectorAll('[data-view]').forEach((button) => {
    if (button.getAttribute('data-view') === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  document.querySelectorAll('.view').forEach((node) => { node.hidden = node.id !== `view-${view}`; });
  if (!state.signedOut) RENDER[view]();
}

// -- wiring ------------------------------------------------------------------------------
document.querySelectorAll('[data-view]').forEach((button) => {
  button.addEventListener('click', () => { show(button.getAttribute('data-view')); $('main').focus(); });
});
$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  const message = $('message').value.trim();
  if (!message || state.live || state.signedOut) return;
  $('message').value = '';
  send(message);
});
$('message').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    $('composer').requestSubmit();
  }
});
$('stop').addEventListener('click', async () => {
  const live = state.live;
  if (!live || !live.requestId) return;
  $('stop').disabled = true;
  live.note.textContent = 'Stopping.';
  try { await api('cancel', { body: { request_id: live.requestId } }); } catch { /* shown */ }
});
$('new-session').addEventListener('click', () => { if (!state.live) loadSession(null); $('message').focus(); });
$('signout').addEventListener('click', async () => {
  try { await api('logout'); } catch { /* signed out anyway */ }
  sessionStorage.removeItem('bsa.csrf');
  connection.signedOut('You signed out.');
});

async function boot() {
  if (!csrf) { connection.signedOut('This tab is not signed in.'); return; }
  try {
    await api('whoami');
    await poll();
    show('chat');
    performance.mark('bsa-first-render');
    window.__bsa.firstRender = performance.now();
  } catch { /* shown by api() */ }
  setInterval(poll, 5000);
}

boot();
