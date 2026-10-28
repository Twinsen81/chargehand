// The read-only board. It polls /api/board and patches the page in place.
//
// Every value from the API is untrusted text: it is set with textContent only, and a link
// is made only from a plain http(s) URL. Columns and cards are kept between polls and
// updated in place, keyed by worker id, so the scroll position of each lane stays.
'use strict';

const POLL_MS = 1000;
const STALE_AFTER_MS = 5000;
const $ = (id) => document.getElementById(id);
let lastOk = 0;
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
  text.title = h.last_tick_error || '';
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
  return nodes;
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
  const now = model.generated_at;
  const usage = model.usage || {};
  renderGauge('five_hour', usage.known ? usage.five_hour : null);
  renderGauge('seven_day', usage.known ? usage.seven_day : null);
  renderThrottle(model.throttle);
  renderHealth(model.health, now);
  renderStats(model.columns);
  renderColumns(model.columns, now);
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

poll();
