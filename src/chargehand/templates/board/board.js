// The board. It polls /api/board and patches the page in place, and sends actions.
//
// Every value from the API is untrusted text: it is set with textContent only, and a link
// is made only from a plain http(s) URL. Columns and cards are kept between polls and
// updated in place, keyed by worker id, so the scroll position of each lane stays.
//
// An action is a POST with the daemon's token, which the operator pastes once and this
// browser keeps in localStorage for this origin. It is sent only in the Authorization
// header, never in a URL. The board has no chat: a conversation goes through attach.
//
// A permission prompt shows on its card with Allow, Deny and Always allow while the
// session's hook waits in the daemon; after that only the session can answer it.
//
// Within a column the cards are grouped by repository. The names, icons, pins and the
// collapsed and hidden repositories are the board's own state in the ledger; the daemon
// sorts and filters by them, and this page only draws what it gets.
//
// An archive runs as a job on a thread of the daemon; the top bar and the card show its
// state and progress, and the page asks the daemon for nothing else while it runs.
'use strict';

const POLL_MS = 1000;
const STALE_AFTER_MS = 5000;
// How long the page follows a request that the daemon has not applied yet.
const FOLLOW_MS = 15 * 60 * 1000;
const TOKEN_KEY = 'chargehand.token';
const TERMINAL_KEY = 'chargehand.terminal';
const REPLY_MAX = 500;
const SHOW_HIDDEN_KEY = 'chargehand.showHidden';
const NAME_MAX = 60;
const BOARD_PIN_HINT = 'Board pin: this sorts the card first and keeps it visible in a collapsed ' +
  'repository. It does not keep the session running.';
const ACTION_LABELS = {
  continue: 'Continue', pause: 'Pause', cancel: 'Cancel', reply: 'Reply',
  attach: 'Attach', retry: 'Retry', discard: 'Discard', archive: 'Archive',
};
const FORCED = new Set(['continue', 'retry', 'discard', 'archive']);
const ANSWER_LABELS = { allow: 'Allow', deny: 'Deny', always: 'Always allow' };
const ANSWER_HINTS = {
  allow: 'Allow this tool call once',
  deny: 'Deny this tool call; the session goes on without it',
  always: 'Allow it, and allow the same tool with the same input for this worker without asking. ' +
    'The role, the repository and the Claude Code settings do not change.',
};
const NOT_HELD_NOTE = 'The session no longer waits for the board: attach to it and answer there.';
const FORCE_HINT = 'continue: accept the files in the worktree as they are now; ' +
  'retry, discard and archive: throw away unpushed or uncommitted work';
const ARCHIVE_NOTE = 'Its worktree, the transcript and its records are zipped under the state ' +
  'directory, then the worktree and the session are removed.';
const $ = (id) => document.getElementById(id);
let lastOk = 0;
let latest = null;
// The last rendered signature of each card, by worker id.
const signatures = new Map();
// The repository headings of each column, by column key and repository name.
const groupNodes = new Map();

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

function renderThrottle(t, limit) {
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
  if (limit && limit.model) {
    const badge = el('span', 'model-limit', limit.model + ' limit' +
      (typeof limit.reset_at === 'number' ? ' until ' + clock(limit.reset_at, true) : ''));
    badge.title = 'A turn failed on the ' + limit.model + ' model limit; the usage windows do not show it';
    nodes.push(badge);
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

function renderHidden(hidden) {
  const box = $('hidden-filter');
  const count = hidden ? hidden.count : 0;
  box.hidden = !count && !(hidden && hidden.shown);
  $('hidden-count').textContent = count + ' hidden';
  box.title = count ? 'Hidden: ' + hidden.repositories.join(', ') +
    '. Their workers still run and notify.' : 'No hidden repository';
}

function jobText(job) {
  const what = job.kind + (job.handle ? ' ' + job.handle : '');
  if (job.error) return what + ': ' + job.state + ' · ' + job.error;
  return what + ': ' + job.state + (job.progress ? ' · ' + job.progress : '');
}

function renderJobs(jobs) {
  const box = $('jobs');
  const nodes = (jobs || []).map((job) => {
    const span = el('span', 'job ' + job.state, jobText(job));
    span.title = jobText(job) + (job.result_path ? ' · ' + job.result_path : '');
    return span;
  });
  box.replaceChildren(...nodes);
  box.hidden = !nodes.length;
}

function renderStats(columns) {
  const count = (key) => (columns.find((c) => c.key === key) || { count: 0 }).count;
  const nodes = [['needs_you', ' need you'], ['working', ' working'], ['queued', ' queued'], ['parked', ' parked'], ['idle', ' idle']]
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
  const repo = el('span', 'chip repo', c.repository_label || c.repository);
  repo.title = 'repository ' + c.repository;
  top.append(role, repo);
  const shown = c.session_state && c.session_state !== c.state ? c.state + ' · ' + c.session_state : c.state;
  top.append(el('span', 'chip state ' + c.state, shown));
  if (c.pinned) {
    // Claude Code has a pin of its own that keeps a session running; this one only sorts.
    const pin = el('span', 'chip pin', 'board pin');
    pin.title = BOARD_PIN_HINT;
    top.append(pin);
  }
  const age = el('span', 'age', ago(c.updated_at, now));
  age.dataset.ts = String(c.updated_at);
  top.append(age);
  nodes.push(top);

  const title = el('div', 'title');
  if (c.icon_glyph) title.append(el('span', 'icon', c.icon_glyph));
  title.append(c.display_name ? el('span', null, c.display_name) : link(c.item_url, c.item));
  // Workers on one item share the title; the handle tells them apart.
  if (c.handle) title.append(el('span', 'handle', c.handle));
  nodes.push(title);

  const meta = el('div', 'meta');
  if (c.display_name) meta.append(link(c.item_url, c.item));
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
    nodes.push(promptNode(c));
  } else if (c.waiting_for) {
    nodes.push(el('div', 'alert', c.waiting_for));
  }
  if (c.problem) nodes.push(el('div', 'note', c.problem));
  if (c.state === 'parked') {
    nodes.push(el('div', 'note', typeof c.reset_at === 'number'
      ? 'parked until ' + clock(c.reset_at, true)
      : 'parked; no reset time given, continue resumes it'));
  }
  if (c.queue_reason) nodes.push(el('div', 'note', c.queue_reason));
  if (c.job && (c.job.active || c.job.error)) {
    const job = el('div', 'note job ' + c.job.state, jobText(c.job));
    if (c.job.result_path) job.title = c.job.result_path;
    nodes.push(job);
  }
  if (typeof c.archived_at === 'number') {
    const archived = el('div', 'note', 'archived ' + ago(c.archived_at, now) + ' ago' +
      (c.job && c.job.result_path ? ' to ' + c.job.result_path : ''));
    if (c.job && c.job.result_path) archived.title = c.job.result_path;
    nodes.push(archived);
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
  nodes.push(cardActions(c));
  return nodes;
}

function promptNode(c) {
  const p = c.permission_prompt;
  const box = el('div', 'prompt');
  box.append(el('div', 'alert', 'permission: ' + p.tool_name));
  if (p.tool_input) {
    const input = el('pre', 'input', p.tool_input);
    input.title = p.tool_input;
    box.append(input);
  }
  if (!p.answers || !p.answers.length) {
    box.append(el('div', 'note', NOT_HELD_NOTE));
    return box;
  }
  const row = el('div', 'answers');
  for (const answer of p.answers) {
    const label = ANSWER_LABELS[answer] || answer;
    const button = boardButton(label, ANSWER_HINTS[answer] || '', () => send(
      '/api/workers/' + encodeURIComponent(c.id) + '/answer', { prompt: p.id, answer },
      label.toLowerCase() + ' ' + p.tool_name + ' for ' + c.handle));
    button.className = 'answer ' + answer;
    row.append(button);
  }
  box.append(row);
  return box;
}

function boardButton(text, title, onClick) {
  const button = el('button', null, text);
  button.type = 'button';
  button.title = title;
  button.addEventListener('click', onClick);
  return button;
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
  for (const name of c.actions || []) {
    const button = el('button', 'action ' + name, ACTION_LABELS[name] || name);
    button.type = 'button';
    button.addEventListener('click', () => workerAction(c, name, Boolean(force && force.checked)));
    row.append(button);
  }
  const own = el('span', 'board');
  own.append(
    boardButton(c.pinned ? 'Unpin' : 'Pin', BOARD_PIN_HINT,
      () => personalise('worker', c.id, { pinned: !c.pinned }, (c.pinned ? 'unpin ' : 'pin ') + c.handle)),
    boardButton('Edit', 'Name, icon and board pin of this card; the handle stays',
      () => openPersonalise('worker', c.id, c, c.handle)),
  );
  row.append(own);
  return row;
}

// ----- repository groups -----------------------------------------------------

function groupChildren(g) {
  const nodes = [];
  const toggle = boardButton(g.collapsed ? '▸' : '▾',
    g.collapsed ? 'Show every card of this repository' : 'Show only the pinned cards of this repository',
    () => personalise('repository', g.repository, { collapsed: !g.collapsed },
      (g.collapsed ? 'expand ' : 'collapse ') + g.repository));
  toggle.className = 'toggle';
  nodes.push(toggle);
  if (g.icon_glyph) nodes.push(el('span', 'icon', g.icon_glyph));
  nodes.push(el('span', 'label', g.display_name || g.repository));
  if (g.display_name) nodes.push(el('span', 'configured', g.repository));
  if (g.pinned) {
    const pin = el('span', 'chip pin', 'board pin');
    pin.title = 'Pinned repositories come first in each column';
    nodes.push(pin);
  }
  if (g.hidden) nodes.push(el('span', 'chip flag', 'hidden'));
  nodes.push(el('span', 'count', String(g.count)));
  if (g.folded) nodes.push(el('span', 'folded', g.folded + ' folded'));
  const repository = ((latest && latest.repositories) || []).find((r) => r.name === g.repository);
  if (repository) {
    nodes.push(boardButton('Edit', 'Name, icon, pin, collapse and hide this repository on the board',
      () => openPersonalise('repository', g.repository, repository, g.repository)));
    nodes[nodes.length - 1].className = 'edit';
  }
  return nodes;
}

function groupNode(columnKey, g) {
  const key = columnKey + '\n' + g.repository;
  let entry = groupNodes.get(key);
  if (!entry) {
    const node = el('div', 'group');
    node.dataset.group = g.repository;
    entry = { node, sig: null };
    groupNodes.set(key, entry);
  }
  const repository = ((latest && latest.repositories) || []).find((r) => r.name === g.repository);
  const sig = JSON.stringify([g, repository || null]);
  if (entry.sig !== sig) {
    entry.node.className = 'group' + (g.hidden ? ' hidden-repository' : '');
    entry.node.replaceChildren(...groupChildren(g));
    entry.sig = sig;
  }
  return entry.node;
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
    $('count-' + column.key).textContent = String(column.count);
    // A heading per repository, then its cards, in the order the daemon chose.
    const wanted = [];
    for (const g of column.groups || []) {
      wanted.push(groupNode(column.key, g));
      for (const c of column.cards) {
        if (c.repository !== g.repository) continue;
        const id = String(c.id);
        let card = document.querySelector('.card[data-id="' + CSS.escape(id) + '"]');
        if (!card) {
          card = el('div', 'card');
          card.dataset.id = id;
        }
        const sig = signature(c);
        if (signatures.get(id) !== sig) {
          card.className = 'card' + (c.hidden ? ' hidden-repository' : '');
          card.replaceChildren(...cardChildren(c, now));
          signatures.set(id, sig);
        }
        wanted.push(card);
      }
    }
    wanted.forEach((node, i) => {
      if (container.children[i] !== node) container.insertBefore(node, container.children[i] || null);
    });
    const keep = new Set(wanted);
    for (const node of [...container.querySelectorAll('.card, .group')]) {
      if (keep.has(node)) continue;
      node.remove();
      if (node.dataset.id) signatures.delete(node.dataset.id);
      if (node.dataset.group !== undefined) groupNodes.delete(column.key + '\n' + node.dataset.group);
    }
    const empty = $('empty-' + column.key);
    container.append(empty);
    empty.hidden = wanted.length > 0;
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
  renderThrottle(model.throttle, model.model_limit);
  renderHidden(model.hidden);
  renderJobs(model.jobs);
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
  if (payload.prompt !== undefined) {
    // The waiting hook got the answer at once; no request waits for the daemon.
    notice(what + ': ' + (payload.message || 'ok'), 'ok');
    return;
  }
  if (payload.settings) {
    // A board setting is written at once; no request waits for the daemon.
    notice(what + ': saved', 'ok');
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
  if (name === 'archive' &&
      !window.confirm('Archive ' + c.handle + '? ' + (c.column === 'closed' ? '' : 'It is cancelled first. ') +
        ARCHIVE_NOTE + (force ? ' Unpushed commits and uncommitted changes are not kept as commits.' : ''))) return;
  send(path, FORCED.has(name) ? { force } : {}, what + (force && FORCED.has(name) ? ' --force' : ''));
}

function personalise(kind, key, body, what) {
  const base = kind === 'worker' ? '/api/workers/' : '/api/repositories/';
  send(base + encodeURIComponent(key) + '/board', body, what);
}

function checkbox(checked, title) {
  const input = el('input');
  input.type = 'checkbox';
  input.checked = Boolean(checked);
  if (title) input.title = title;
  return input;
}

// The name, icon and flags of a repository or a worker. *current* is its entry in the model.
function openPersonalise(kind, key, current, label) {
  const panel = $('panel');
  const name = el('input');
  name.type = 'text';
  name.maxLength = NAME_MAX;
  name.value = current.display_name || '';
  name.placeholder = kind === 'worker' ? 'shown instead of the item' : 'shown instead of ' + key;
  const icons = (latest && latest.icons) || {};
  const icon = el('select');
  const none = el('option', null, 'none');
  none.value = '';
  icon.append(none);
  for (const [iconName, glyph] of Object.entries(icons)) {
    const option = el('option', null, glyph + ' ' + iconName);
    option.value = iconName;
    icon.append(option);
  }
  const emoji = el('input');
  emoji.type = 'text';
  emoji.className = 'icon';
  emoji.maxLength = 16;
  emoji.placeholder = 'or one emoji';
  if (current.icon && current.icon in icons) icon.value = current.icon;
  else if (current.icon) emoji.value = current.icon;
  const flags = kind === 'worker' ? ['pinned'] : ['pinned', 'collapsed', 'hidden'];
  const hints = {
    pinned: kind === 'worker' ? BOARD_PIN_HINT : 'Pinned repositories come first in each column',
    collapsed: 'Show only the pinned cards of this repository',
    hidden: 'Leave its cards off the board; its workers still run and notify',
  };
  const boxes = {};
  const fields = [field('name', name), field('icon', icon), field('emoji', emoji)];
  for (const flag of flags) {
    boxes[flag] = checkbox(current[flag], hints[flag]);
    fields.push(field(flag === 'pinned' ? 'board pin' : flag, boxes[flag]));
  }
  const submit = () => {
    const body = { name: name.value.trim() || null, icon: emoji.value.trim() || icon.value || null };
    for (const flag of flags) body[flag] = boxes[flag].checked;
    closePanel();
    personalise(kind, key, body, 'board settings of ' + label);
  };
  name.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') submit();
    if (event.key === 'Escape') closePanel();
  });
  const extra = kind === 'repository' ? [archiveRow(key, current)] : [];
  panel.replaceChildren(el('h3', null, (kind === 'worker' ? 'Card ' : 'Repository ') + label),
    ...fields, panelButtons('Save', submit), ...extra);
  panel.hidden = false;
  name.focus();
}

// Archive hides the repository and archives each worker it still holds; unarchive only
// shows it again. Restoring a worktree from a zip is a manual step.
function archiveRow(name, current) {
  const row = el('div', 'buttons archive');
  const label = el('label', 'force');
  const force = checkbox(false, 'Cancel the live workers first, and archive work that is not pushed');
  label.append(force, ' force');
  const path = '/api/repositories/' + encodeURIComponent(name) + '/';
  const archive = boardButton('Archive repository',
    'Hide it, and archive each of its workers: ' + ARCHIVE_NOTE, () => {
      const how = force.checked ? ' Its live workers are cancelled first, and work that is not pushed is not kept as commits.' : '';
      if (!window.confirm('Archive repository ' + name + '? It is hidden, and each of its workers is archived. ' +
        ARCHIVE_NOTE + how)) return;
      closePanel();
      send(path + 'archive', { force: force.checked }, 'archive repository ' + name + (force.checked ? ' --force' : ''));
    });
  row.append(label, archive);
  if (current.hidden) {
    row.append(boardButton('Unarchive', 'Show the repository on the board again; the archives stay', () => {
      closePanel();
      send(path + 'unarchive', {}, 'unarchive repository ' + name);
    }));
  }
  return row;
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
  const showHidden = $('show-hidden');
  showHidden.checked = localStorage.getItem(SHOW_HIDDEN_KEY) === '1';
  showHidden.addEventListener('change', () => {
    if (showHidden.checked) localStorage.setItem(SHOW_HIDDEN_KEY, '1');
    else localStorage.removeItem(SHOW_HIDDEN_KEY);
  });
  $('new-worker').addEventListener('click', openNewWorker);
  $('pause-all').addEventListener('click', () => send('/api/pause', {}, 'pause all repositories'));
  $('resume-all').addEventListener('click', () => send('/api/resume', {}, 'resume all repositories'));
}

async function poll() {
  try {
    const filter = $('show-hidden').checked ? '?show_hidden=1' : '';
    const response = await fetch('/api/board' + filter, { cache: 'no-store', credentials: 'omit' });
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
