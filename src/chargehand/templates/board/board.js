// The board. It polls /api/board and patches the page in place, and sends actions.
//
// Every value from the API is untrusted text: it is set with textContent only, and a link
// is made only from a plain http(s) URL. Columns and cards are kept between polls and
// updated in place, keyed by worker id, so the scroll position of each lane stays.
//
// An action is a POST with the daemon's token, which the operator pastes once and this
// browser keeps in localStorage for this origin. It is sent only in the Authorization
// header, never in a URL. The board has no chat: a conversation goes through attach.
'use strict';

const POLL_MS = 1000;
const STALE_AFTER_MS = 5000;
// How long the page follows a request that the daemon has not applied yet.
const FOLLOW_MS = 15 * 60 * 1000;
const TOKEN_KEY = 'chargehand.token';
const TERMINAL_KEY = 'chargehand.terminal';
const REPLY_MAX = 500;
const ACTION_LABELS = {
  continue: 'Continue', pause: 'Pause', cancel: 'Cancel', reply: 'Reply',
  attach: 'Attach', retry: 'Retry', discard: 'Discard',
};
const FORCED = new Set(['continue', 'retry', 'discard']);
const FORCE_HINT = 'continue: accept the files in the worktree as they are now; ' +
  'retry and discard: throw away unpushed or uncommitted work';
const $ = (id) => document.getElementById(id);
let lastOk = 0;
let latest = null;
// The last rendered signature of each card, by worker id.
const signatures = new Map();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function safeUrl(value) {
  return typeof value === 'string' && /^https?:\/\/[^\s]+$/i.test(value) ? value : null;
}

function link(url, text) {
  const href = safeUrl(url);
  if (!href) return el('span', null, text);
  const a = el('a', null, text);
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}

function ago(epochSecs, nowSecs) {
  if (typeof epochSecs !== 'number') return '';
  const s = Math.max(0, nowSecs - epochSecs);
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return (s / 3600).toFixed(s < 36000 ? 1 : 0) + 'h';
  return Math.round(s / 86400) + 'd';
}

function clock(epochSecs, withDay) {
  if (typeof epochSecs !== 'number') return '';
  const date = new Date(epochSecs * 1000);
  const options = { hour: '2-digit', minute: '2-digit' };
  if (withDay) options.weekday = 'short';
  return date.toLocaleString(undefined, options);
}

function tokens(n) {
  if (typeof n !== 'number') return '-';
  if (n < 1000) return '~' + n;
  if (n < 1000000) return '~' + Math.round(n / 1000) + 'k';
  return '~' + (n / 1000000).toFixed(1) + 'M';
}

function hue(text) {
  let h = 0;
  for (const c of String(text)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h % 360;
}

// ----- the top bar -----------------------------------------------------------

function renderGauge(name, window) {
  const gauge = $('gauge-' + name);
  if (!gauge) return;
  const used = window && typeof window.used_percent === 'number' ? window.used_percent : null;
  const level = used === null ? 'grey' : (window.level || 'grey');
  gauge.className = 'gauge ' + level;
  gauge.querySelector('.fill').style.width = (used === null ? 0 : Math.min(100, used)) + '%';
  gauge.querySelector('.value').textContent = used === null ? '-' : Math.round(used) + '%';
  const reset = used === null ? '' : clock(window.resets_at, name === 'seven_day');
  gauge.querySelector('.reset').textContent = reset ? 'resets ' + reset : '';
}

function renderThrottle(t) {
  const box = $('throttle');
  const parts = [
    [t.working + '/' + t.max_concurrent, ' working'],
    [t.open + '/' + t.max_open_workers, ' open'],
  ];
  const nodes = parts.map(([value, label]) => {
    const span = el('span');
    span.append(el('b', null, value), label);
    return span;
  });
  if (t.paused) nodes.push(el('span', 'paused', 'launches paused'));
  if (t.paused_repositories && t.paused_repositories.length) {
    nodes.push(el('span', 'paused', 'paused: ' + t.paused_repositories.join(', ')));
  }
  box.replaceChildren(...nodes);
}

function renderHealth(h, now) {
  const box = $('health');
  const parts = [];
  if (h.ticking) parts.push('ticking');
  else if (typeof h.last_tick_at === 'number') {
    parts.push('tick ' + ago(h.last_tick_at, now) + ' ago' +
      (typeof h.last_tick_secs === 'number' ? ' (' + h.last_tick_secs.toFixed(1) + 's)' : ''));
  } else parts.push('no tick yet');
  if (typeof h.started_at === 'number') parts.push('up ' + ago(h.started_at, now));
  if (h.failed_ticks) parts.push(h.failed_ticks + ' failed');
  if (h.version) parts.push('v' + h.version);
  if (h.last_tick_error) parts.push('error: ' + h.last_tick_error);
  box.className = 'health' + (h.last_tick_error ? ' bad' : '');
  const text = box.querySelector('.text');
  text.textContent = parts.join(' · ');
  text.title = parts.join(' · ');
}

function renderStats(columns) {
  const count = (key) => (columns.find((c) => c.key === key) || { cards: [] }).cards.length;
  const nodes = [['needs_you', ' need you'], ['working', ' working'], ['queued', ' queued'], ['idle', ' idle']]
    .map(([key, label]) => {
      const span = el('span');
      span.append(el('b', null, count(key)), label);
      return span;
    });
  $('stats').replaceChildren(...nodes);
}

// ----- columns and cards -----------------------------------------------------

function ensureColumns(columns) {
  const board = $('board');
  for (const column of columns) {
    if ($('cards-' + column.key)) continue;
    const section = el('section', 'column');
    const head = el('div', 'head');
    const title = el('h2');
    title.append(el('span', 'dot ' + column.key), column.title);
    const count = el('span', 'count', '0');
    count.id = 'count-' + column.key;
    title.append(count);
    head.append(title, el('span', 'hint', column.hint));
    const cards = el('div', 'cards');
    cards.id = 'cards-' + column.key;
    const empty = el('div', 'empty', 'Nothing here');
    empty.id = 'empty-' + column.key;
    cards.append(empty);
    section.append(head, cards);
    board.append(section);
  }
}

function snippet(kind, label, text) {
  const node = el('div', 'snippet ' + kind);
  node.append(el('span', 'who', label + ': '), text);
  node.title = text;
  return node;
}

function cardChildren(c, now) {
  const nodes = [];

  const top = el('div', 'top');
  top.append(el('span', 'dot ' + c.column));
  const role = el('span', 'chip role', c.role);
  role.style.background = 'hsl(' + hue(c.role) + ' 45% 38%)';
  top.append(role, el('span', 'chip repo', c.repository));
  const shown = c.session_state && c.session_state !== c.state ? c.state + ' · ' + c.session_state : c.state;
  top.append(el('span', 'chip state ' + c.state, shown));
  const age = el('span', 'age', ago(c.updated_at, now));
  age.dataset.ts = String(c.updated_at);
  top.append(age);
  nodes.push(top);

  const title = el('div', 'title');
  title.append(link(c.item_url, c.item));
  // Workers on one item share the title; the handle tells them apart.
  if (c.handle) title.append(el('span', 'handle', c.handle));
  nodes.push(title);

  const meta = el('div', 'meta');
  if (c.branch) {
    const branch = el('span', null, '⎇ ' + c.branch);
    branch.title = c.branch;
    meta.append(branch);
  }
  meta.append(el('span', null, (c.model || 'default model') + (c.effort ? ' · ' + c.effort : '')));
  nodes.push(meta);

  if (c.worktree) {
    const path = el('div', 'path', c.worktree);
    path.title = c.worktree;
    nodes.push(path);
  }

  if (c.permission_prompt) {
    const p = c.permission_prompt;
    nodes.push(el('div', 'alert', 'permission: ' + p.tool_name + (p.tool_input ? ' ' + p.tool_input : '')));
  } else if (c.waiting_for) {
    nodes.push(el('div', 'alert', c.waiting_for));
  }
  if (c.problem) nodes.push(el('div', 'note', c.problem));
  if (c.state === 'parked' && typeof c.reset_at === 'number') {
    nodes.push(el('div', 'note', 'parked until ' + clock(c.reset_at, true)));
  }

  if (c.last_prompt) nodes.push(snippet('prompt', 'prompt', c.last_prompt));
  if (c.last_answer) nodes.push(snippet('answer', 'answer', c.last_answer));

  const foot = el('div', 'foot');
  foot.append(el('span', null, 'wakes ' + c.wakes_today + (typeof c.wake_cap === 'number' ? '/' + c.wake_cap : '') + ' today'));
  const tok = el('span', null, tokens(c.tokens_estimate) + ' tokens');
  tok.title = 'Estimated from the transcript size; approximate';
  foot.append(tok);
  if (safeUrl(c.pr_url)) foot.append(link(c.pr_url, 'pull request'));
  nodes.push(foot);

  if (c.attach) {
    const attach = el('div', 'attach', c.attach);
    attach.title = c.attach;
    nodes.push(attach);
  }
  if (c.actions && c.actions.length) nodes.push(cardActions(c));
  return nodes;
}

function cardActions(c) {
  const row = el('div', 'actions');
  let force = null;
  if (c.actions.some((name) => FORCED.has(name))) {
    const label = el('label', 'force');
    force = el('input');
    force.type = 'checkbox';
    force.title = FORCE_HINT;
    label.append(force, ' force');
    label.title = FORCE_HINT;
    row.append(label);
  }
  for (const name of c.actions) {
    const button = el('button', 'action ' + name, ACTION_LABELS[name] || name);
    button.type = 'button';
    button.addEventListener('click', () => workerAction(c, name, Boolean(force && force.checked)));
    row.append(button);
  }
  return row;
}

function signature(c) {
  // The age is refreshed separately, so it is not part of the signature.
  const { updated_at, ...rest } = c;
  return JSON.stringify(rest);
}

function renderColumns(columns, now) {
  ensureColumns(columns);
  for (const column of columns) {
    const container = $('cards-' + column.key);
    $('count-' + column.key).textContent = String(column.cards.length);
    const keep = new Set();
    column.cards.forEach((c, i) => {
      const id = String(c.id);
      keep.add(id);
      let card = document.querySelector('.card[data-id="' + CSS.escape(id) + '"]');
      if (!card) {
        card = el('div', 'card');
        card.dataset.id = id;
      }
      const sig = signature(c);
      if (signatures.get(id) !== sig) {
        card.replaceChildren(...cardChildren(c, now));
        signatures.set(id, sig);
      }
      if (container.children[i] !== card) container.insertBefore(card, container.children[i] || null);
    });
    for (const card of [...container.querySelectorAll('.card')]) {
      if (!keep.has(card.dataset.id)) {
        card.remove();
        signatures.delete(card.dataset.id);
      }
    }
    const empty = $('empty-' + column.key);
    container.append(empty);
    empty.hidden = column.cards.length > 0;
  }
  for (const age of document.querySelectorAll('.card .age[data-ts]')) {
    age.textContent = ago(Number(age.dataset.ts), now);
  }
}

// ----- polling ---------------------------------------------------------------

function render(model) {
  latest = model;
  const now = model.generated_at;
  const usage = model.usage || {};
  renderGauge('five_hour', usage.known ? usage.five_hour : null);
  renderGauge('seven_day', usage.known ? usage.seven_day : null);
  renderThrottle(model.throttle);
  renderHealth(model.health, now);
  renderStats(model.columns);
  renderControls(model);
  renderColumns(model.columns, now);
}

// ----- controls and actions --------------------------------------------------

let controlsSignature = null;

function renderControls(model) {
  const sig = JSON.stringify([model.repositories, model.terminals]);
  if (sig === controlsSignature) return;
  controlsSignature = sig;
  const nodes = (model.repositories || []).map((repository) => {
    const verb = repository.paused ? 'resume' : 'pause';
    const button = el('button', 'repository ' + verb,
      (repository.paused ? 'Resume ' : 'Pause ') + repository.name);
    button.type = 'button';
    button.title = repository.paused ? 'Admit launches in this repository again'
      : 'Stop admitting launches in this repository';
    button.addEventListener('click', () => send(
      '/api/repositories/' + encodeURIComponent(repository.name) + '/' + verb, {},
      verb + ' ' + repository.name));
    return button;
  });
  $('repositories').replaceChildren(...nodes);
  const select = $('terminal');
  const chosen = localStorage.getItem(TERMINAL_KEY);
  select.replaceChildren(...(model.terminals || []).map((name) => {
    const option = el('option', null, name === 'iterm2' ? 'iTerm2' : name === 'terminal' ? 'Terminal' : name);
    option.value = name;
    option.selected = name === chosen;
    return option;
  }));
}

function token() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

function notice(text, kind) {
  const box = $('notice');
  box.className = 'notice ' + (kind || '');
  box.textContent = text;
  box.hidden = !text;
}

function describe(outcome) {
  const what = outcome.kind + (outcome.target ? ' ' + outcome.target : '');
  if (outcome.pending) return what + ': recorded, waiting for the daemon to apply it';
  return what + ': ' + (outcome.ok ? 'ok' : 'refused') + (outcome.message ? ' · ' + outcome.message : '');
}

async function follow(outcome) {
  const deadline = Date.now() + FOLLOW_MS;
  let current = outcome;
  while (current.pending && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    try {
      const response = await fetch('/api/requests/' + encodeURIComponent(current.id),
        { cache: 'no-store', credentials: 'omit' });
      if (response.ok) current = await response.json();
    } catch (error) {
      // The daemon may be restarting; keep asking until the deadline.
    }
    notice(describe(current), current.pending ? 'pending' : current.ok ? 'ok' : 'bad');
  }
}

async function send(path, body, what) {
  if (!token()) {
    notice('Paste the token first: the content of ' + ((latest && latest.token_file) || 'the token file') + '.', 'bad');
    $('token').focus();
    return;
  }
  notice(what + ': sending', 'pending');
  let response;
  try {
    response = await fetch(path, {
      method: 'POST',
      cache: 'no-store',
      credentials: 'omit',
      headers: { 'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  } catch (error) {
    notice(what + ': the daemon is unreachable', 'bad');
    return;
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    payload = {};
  }
  if (response.status === 401) {
    notice('The token is not valid. Paste the content of ' + ((latest && latest.token_file) || 'the token file') + '.', 'bad');
    return;
  }
  if (!response.ok) {
    notice(what + ': ' + (payload.error || 'HTTP ' + response.status), 'bad');
    return;
  }
  notice(describe(payload), payload.pending ? 'pending' : payload.ok ? 'ok' : 'bad');
  if (payload.pending) follow(payload);
}

function workerAction(c, name, force) {
  const path = '/api/workers/' + encodeURIComponent(c.id) + '/' + name;
  const what = name + ' ' + c.handle;
  if (name === 'reply') {
    openReply(c);
    return;
  }
  if (name === 'attach') {
    send(path, { terminal: $('terminal').value || 'iterm2' }, what);
    return;
  }
  if (name === 'discard' &&
      !window.confirm('Discard ' + c.handle + '? This removes its session, its worktree and its local branch' +
        (force ? ', even with unpushed or uncommitted work.' : '.'))) return;
  if (name === 'cancel' && !window.confirm('Cancel ' + c.handle + '? Its session stops; the worktree stays.')) return;
  send(path, FORCED.has(name) ? { force } : {}, what + (force && FORCED.has(name) ? ' --force' : ''));
}

function closePanel() {
  const panel = $('panel');
  panel.replaceChildren();
  panel.hidden = true;
}

function field(labelText, input) {
  const label = el('label', 'field');
  label.append(el('span', null, labelText), input);
  return label;
}

function panelButtons(primaryText, onPrimary) {
  const row = el('div', 'buttons');
  const primary = el('button', 'primary', primaryText);
  primary.type = 'button';
  primary.addEventListener('click', onPrimary);
  const close = el('button', null, 'Close');
  close.type = 'button';
  close.addEventListener('click', closePanel);
  row.append(primary, close);
  return row;
}

function openReply(c) {
  const panel = $('panel');
  const input = el('input');
  input.type = 'text';
  input.maxLength = REPLY_MAX;
  input.placeholder = 'One line; it reaches the session as an event from the board';
  const submit = () => {
    const text = input.value.trim();
    if (!text) return;
    closePanel();
    send('/api/workers/' + encodeURIComponent(c.id) + '/reply', { text }, 'reply ' + c.handle);
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') submit();
    if (event.key === 'Escape') closePanel();
  });
  panel.replaceChildren(el('h3', null, 'Reply to ' + c.handle), field('line', input),
    panelButtons('Send', submit));
  panel.hidden = false;
  input.focus();
}

function openNewWorker() {
  const repositories = (latest && latest.repositories) || [];
  const panel = $('panel');
  const repository = el('select');
  for (const entry of repositories) {
    const option = el('option', null, entry.name);
    option.value = entry.name;
    repository.append(option);
  }
  const role = el('select');
  const fillRoles = () => {
    const entry = repositories.find((r) => r.name === repository.value) || { roles: [] };
    role.replaceChildren(...entry.roles.map((name) => {
      const option = el('option', null, name);
      option.value = name;
      return option;
    }));
  };
  repository.addEventListener('change', fillRoles);
  fillRoles();
  const item = el('input');
  item.type = 'text';
  item.placeholder = 'optional, as the tracker names it';
  const kinds = el('input');
  kinds.type = 'text';
  kinds.value = 'comment';
  kinds.title = 'The event kinds on the item that wake the worker, separated by commas';
  const submit = () => {
    const body = { repository: repository.value, role: role.value };
    if (item.value.trim()) {
      body.item = item.value.trim();
      const chosen = kinds.value.split(',').map((k) => k.trim()).filter(Boolean);
      if (chosen.length) body.subscribe = chosen;
    }
    closePanel();
    send('/api/workers', body, 'new worker ' + body.role + (body.item ? ' on ' + body.item : ''));
  };
  panel.replaceChildren(el('h3', null, 'New worker'), field('repository', repository),
    field('role', role), field('work item', item), field('wakes on', kinds),
    panelButtons('Start', submit));
  panel.hidden = false;
}

function setupControls() {
  const input = $('token');
  input.value = token();
  input.addEventListener('change', () => {
    const value = input.value.trim();
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  });
  $('token-forget').addEventListener('click', () => {
    localStorage.removeItem(TOKEN_KEY);
    input.value = '';
  });
  $('terminal').addEventListener('change', () => localStorage.setItem(TERMINAL_KEY, $('terminal').value));
  $('new-worker').addEventListener('click', openNewWorker);
  $('pause-all').addEventListener('click', () => send('/api/pause', {}, 'pause all repositories'));
  $('resume-all').addEventListener('click', () => send('/api/resume', {}, 'resume all repositories'));
}

async function poll() {
  try {
    const response = await fetch('/api/board', { cache: 'no-store', credentials: 'omit' });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const model = await response.json();
    render(model);
    lastOk = Date.now();
    const refresh = $('refresh');
    refresh.textContent = 'updated ' + new Date(model.generated_at * 1000).toLocaleTimeString();
    refresh.classList.remove('stale');
  } catch (error) {
    if (Date.now() - lastOk > STALE_AFTER_MS) {
      $('refresh').textContent = 'daemon unreachable';
      $('refresh').classList.add('stale');
      $('health').className = 'health unknown';
      renderGauge('five_hour', null);
      renderGauge('seven_day', null);
    }
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

setupControls();
poll();
