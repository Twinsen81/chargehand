import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const SESSION = '11111111-2222-3333-4444-555555555555'
const CWD = '/tmp/worktrees/watcher/DEMO-1'
const TOKEN_FILE = '/tmp/chargehand/board.token'

// What the daemon answers on a path it does not reach.
const DOWN = 'down'

type Post = { path: string; body: Record<string, unknown>; authorization: string | undefined }

const TOOLS = [
  {
    name: 'subscribe',
    description: 'Follow the events of a work item.',
    input_schema: { type: 'object', properties: { item: { type: 'string' } }, required: ['item'] },
  },
]

// The daemon on 127.0.0.1:7979, as the test sees it: each post and what it answers.
function daemon(on: On, answers: Record<string, unknown> = {}, registered: string[] = []): Post[] {
  const posts: Post[] = []
  mock.env(on, { CHARGEHAND_MOD_PORT: '7979', CHARGEHAND_MOD_TOKEN_FILE: TOKEN_FILE })
  on('session.id', () => ({ value: SESSION }))
  on('session.cwd', () => ({ value: CWD }))
  on('fs.read', () => ({ value: 'the-token\n' }))
  on('tool.register', (_, e) => {
    registered.push(e.name)
    return { value: { tool: `mcp__chargehand__${e.name}` } }
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('http.fetch', (_, e) => {
    const url = new URL(e.url)
    expect(url.host).toBe('127.0.0.1:7979')
    const body = JSON.parse(String(e.init?.body ?? '{}'))
    posts.push({ path: url.pathname, body, authorization: e.init?.headers?.authorization })
    const answer = url.pathname in answers ? answers[url.pathname] : {}
    if (answer === DOWN) throw new Error('connection refused')
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(answer) } }
  })
  return posts
}

function paths(posts: Post[]): string[] {
  return posts.map(post => post.path)
}

test('the session start registers the daemon tools and reports the start', async ($, on) => {
  const registered: string[] = []
  const posts = daemon(on, { '/channel/hello': { tools: TOOLS, outbox_poll_secs: 5 } }, registered)
  mock.clock(on)

  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  expect(registered).toEqual(['subscribe'])
  expect(paths(posts)).toEqual(['/channel/hello', '/channel/lifecycle/session_start'])
  for (const post of posts) {
    expect(post.authorization).toBe('Bearer the-token')
    expect(post.body.session_id).toBe(SESSION)
    expect(post.body.cwd).toBe(CWD)
  }
})

test('without the launch settings the mod posts nothing', async ($, on) => {
  const fetched: string[] = []
  mock.env(on, {})
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('http.fetch', (_, e) => {
    fetched.push(e.url)
    return { value: { status: 200, ok: true, headers: {}, text: '{}' } }
  })
  const clock = mock.clock(on)

  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })
  await clock.advance(60_000)

  expect(fetched).toEqual([])
})

test('a tool call goes to the daemon and returns its answer', async ($, on) => {
  const posts = daemon(on, {
    '/channel/hello': { tools: TOOLS },
    '/channel/tool/subscribe': { result: 'subscribed to DEMO-2' },
  })
  mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  const answer = await $.tool.call({ tool: 'mcp__chargehand__subscribe', item: 'DEMO-2' } as never)

  expect(answer.result).toBe('subscribed to DEMO-2')
  const call = posts.find(post => post.path === '/channel/tool/subscribe')
  expect(call?.body.input).toEqual({ item: 'DEMO-2' })
})

test('a refused tool call is an error for the model', async ($, on) => {
  daemon(on, {
    '/channel/hello': { tools: TOOLS },
    '/channel/tool/subscribe': { result: 'item is not a plain URL or identifier', is_error: true },
  })
  mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  const refused = await $.tool.call({ tool: 'mcp__chargehand__subscribe', item: '<x>' } as never)

  expect(refused.deny).toBe('item is not a plain URL or identifier')
})

test('a tool call while the daemon is down is an error for the model', async ($, on) => {
  daemon(on, { '/channel/hello': { tools: TOOLS }, '/channel/tool/subscribe': DOWN })
  mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  const answer = await $.tool.call({ tool: 'mcp__chargehand__subscribe', item: 'DEMO-2' } as never)

  expect(answer.deny).toMatch(/not reachable/)
})

test('the outbox poll submits only the text the daemon handed out, and confirms it', async ($, on) => {
  const submitted: string[] = []
  const posts = daemon(on, {
    '/channel/hello': { tools: TOOLS, outbox_poll_secs: 5 },
    '/channel/outbox': { message: { id: 7, text: 'New events on the work you follow.\ncomment: DEMO-1' } },
  })
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  const clock = mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  await clock.advance(5_000)

  expect(submitted).toEqual(['New events on the work you follow.\ncomment: DEMO-1'])
  const confirmed = posts.filter(post => post.path === '/channel/outbox/confirm')
  expect(confirmed.map(post => post.body)).toEqual([
    { id: 7, submitted: true, session_id: SESSION, cwd: CWD },
  ])
})

test('an empty outbox submits nothing', async ($, on) => {
  const submitted: string[] = []
  const posts = daemon(on, {
    '/channel/hello': { tools: TOOLS, outbox_poll_secs: 5 },
    '/channel/outbox': { message: null },
  })
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  const clock = mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  await clock.advance(15_000)

  expect(submitted).toEqual([])
  expect(paths(posts).filter(path => path === '/channel/outbox').length).toBe(3)
})

test('a dropped submission is confirmed as not submitted', async ($, on) => {
  const posts = daemon(on, {
    '/channel/hello': { tools: TOOLS, outbox_poll_secs: 5 },
    '/channel/outbox': { message: { id: 8, text: 'comment: DEMO-1' } },
  })
  on('prompt.submit', () => ({ drop: 'refused by policy' }))
  const clock = mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  await clock.advance(5_000)

  const confirmed = posts.find(post => post.path === '/channel/outbox/confirm')
  expect(confirmed?.body.submitted).toBe(false)
})

test('a turn end posts the lifecycle report and the usage reading', async ($, on) => {
  const posts = daemon(on, { '/channel/hello': { tools: TOOLS } })
  const rateLimits = [
    { kind: 'five_hour', percentUsed: 10, resetsAt: '2026-11-10T20:00:00Z' },
    { kind: 'seven_day', percentUsed: 42.5, resetsAt: '2026-11-14T08:00:00Z' },
  ]
  on('session.usage', () => ({
    value: { startedAt: 0, context: { tokens: 1200, window: 200000, percent: 1 }, rateLimits },
  }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  await $.turn.complete({ answer: 'READY', durationMs: 10, isAborted: false, turnId: 't1', reason: 'answer' })

  const turn = posts.find(post => post.path === '/channel/lifecycle/turn_complete')
  expect(turn?.body.reason).toBe('answer')
  expect(turn?.body.answer).toBe('READY')
  const usage = posts.find(post => post.path === '/channel/usage')
  expect(usage?.body.rate_limits).toEqual(rateLimits)
  expect(usage?.body.context).toEqual({ tokens: 1200, window: 200000, percent: 1 })
})

test('a subagent turn end posts nothing', async ($, on) => {
  const posts = daemon(on, { '/channel/hello': { tools: TOOLS } })
  on('turn.complete', (_, e) => ({ text: e.answer }))
  mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  await $.turn.complete({
    answer: 'report', durationMs: 10, isAborted: false, turnId: 't2', reason: 'answer', agentId: 'a1',
  })

  expect(paths(posts)).toEqual(['/channel/hello', '/channel/lifecycle/session_start'])
})

test('the session end is reported', async ($, on) => {
  const posts = daemon(on, { '/channel/hello': { tools: TOOLS } })
  on('session.end', (_, e) => ({ sessionId: e.sessionId }))
  mock.clock(on)
  await $.session.start({ cwd: CWD, surface: null, isInteractive: false })

  await $.session.end({ reason: 'other', sessionId: SESSION, resume: { id: SESSION } } as never)

  const end = posts.find(post => post.path === '/channel/lifecycle/session_end')
  expect(end?.body.reason).toBe('other')
})
