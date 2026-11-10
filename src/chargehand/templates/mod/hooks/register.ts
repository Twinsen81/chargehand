// The chargehand mod: the transport between a worker session and the chargehand daemon.
//
// It registers the tools the daemon lists, forwards each call to the daemon, takes the
// worker's outbox message and submits it once the session is idle, and reports the start
// and end of the session, each turn end and the usage reading. It never approves a tool
// call, and it submits no prompt but the text the daemon handed it.
//
// The daemon's port and token file come from the session's launch settings. There is no
// default: a session that chargehand did not start posts nowhere.
import type { EngineInterface, Register } from 'claude-code'

const PREFIX = 'mcp__chargehand__'
const TOOL_NAME = /^[a-z][a-z_]{0,39}$/
const PORT = /^[0-9]{1,5}$/
// Until the daemon says otherwise; its hello answer carries its own interval.
const DEFAULT_POLL_MS = 5000
const MIN_POLL_MS = 1000
const ANSWER_LIMIT = 4000
const UNREACHABLE = 'chargehand is not reachable now. Go on without this tool, and try it again later.'

type Channel = { base: string; tokenFile: string }
type Reply = Record<string, unknown>

// A reload starts these over, and runs session.start again.
let channel: Channel | null = null
// Whether the daemon's tools are registered in this load of the module.
let hasTools = false
// An outbox message is submitted and its turn has not started yet: no second one meanwhile.
let isSubmitting = false

async function findChannel($: EngineInterface): Promise<Channel | null> {
  if (channel) return channel
  const port = await $.env.get('CHARGEHAND_MOD_PORT')
  const tokenFile = await $.env.get('CHARGEHAND_MOD_TOKEN_FILE')
  if (!port || !PORT.test(port) || !tokenFile) return null
  channel = { base: `http://127.0.0.1:${port}`, tokenFile }
  return channel
}

async function post($: EngineInterface, path: string, body: Reply): Promise<Reply | null> {
  const found = await findChannel($)
  if (!found) return null
  try {
    // Read for every post: the daemon may have written a new token since the last one.
    const token = (await $.fs.read(found.tokenFile)).trim()
    if (!token) return null
    const response = await $.http.fetch(`${found.base}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
      // The session id is read each time: after a /clear the process goes on under a new one.
      body: JSON.stringify({ ...body, session_id: await $.session.id(), cwd: await $.session.cwd() }),
    })
    if (!response.ok) return null
    const reply: unknown = JSON.parse(response.text || '{}')
    return reply && typeof reply === 'object' ? (reply as Reply) : null
  } catch {
    return null
  }
}

// Registers the daemon's tools. Resolves with how often to poll the outbox, in
// milliseconds, or null when the daemon did not answer.
async function hello($: EngineInterface): Promise<number | null> {
  const reply = await post($, '/channel/hello', {})
  const tools = reply && Array.isArray(reply.tools) ? reply.tools : null
  if (!reply || !tools) return null
  for (const tool of tools) {
    if (!tool || typeof tool !== 'object') continue
    const { name, description, input_schema: inputSchema } = tool as Reply
    if (typeof name !== 'string' || !TOOL_NAME.test(name) || typeof description !== 'string') continue
    if (!inputSchema || typeof inputSchema !== 'object') continue
    try {
      await $.tool.register({
        name,
        description,
        inputSchema: inputSchema as Record<string, unknown>,
        isDeferred: false,
      })
    } catch {
      // The other tools still register.
    }
  }
  hasTools = true
  const seconds = typeof reply.outbox_poll_secs === 'number' ? reply.outbox_poll_secs : 0
  return seconds > 0 ? Math.max(MIN_POLL_MS, seconds * 1000) : DEFAULT_POLL_MS
}

async function confirm($: EngineInterface, id: number, submitted: boolean): Promise<void> {
  await post($, '/channel/outbox/confirm', { id, submitted })
}

async function poll($: EngineInterface): Promise<void> {
  // A daemon that was down at the session start gets the hello from the first poll that
  // reaches it.
  if (!hasTools) await hello($)
  if (isSubmitting) return
  const reply = await post($, '/channel/outbox', {})
  const message = reply && reply.message
  if (!message || typeof message !== 'object') return
  const { id, text } = message as Reply
  if (typeof id !== 'number' || typeof text !== 'string' || !text) return
  isSubmitting = true
  try {
    // Resolves once the turn starts; the session starts it when it is idle.
    const result = await $.prompt.submit({ text, asUser: true })
    await confirm($, id, result.drop === undefined)
  } catch {
    await confirm($, id, false)
  } finally {
    isSubmitting = false
  }
}

async function reportTurn($: EngineInterface, reason: string, answer: string): Promise<void> {
  await post($, '/channel/lifecycle/turn_complete', { reason, answer: answer.slice(0, ANSWER_LIMIT) })
  try {
    const usage = await $.session.usage()
    await post($, '/channel/usage', {
      rate_limits: usage.rateLimits,
      context: { tokens: usage.context.tokens, window: usage.context.window, percent: usage.context.percent },
    })
  } catch {
    // No reading this time; the next turn sends one.
  }
}

async function callTool($: EngineInterface, tool: string, input: Reply) {
  const name = tool.slice(PREFIX.length)
  if (!TOOL_NAME.test(name)) return { deny: 'chargehand has no such tool.' }
  const reply = await post($, `/channel/tool/${name}`, { input })
  if (!reply) return { deny: UNREACHABLE }
  const text = typeof reply.result === 'string' ? reply.result : ''
  return reply.is_error === true ? { deny: text || 'chargehand refused the call.' } : { result: text }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const every = (await hello($)) ?? DEFAULT_POLL_MS
    await post($, '/channel/lifecycle/session_start', {})
    $.clock.every(every, () => poll($))
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    // A subagent's turn is not the worker's.
    if (e.agentId === undefined) await reportTurn($, e.reason, e.answer)
    return result
  })

  on('session.end', async ($, e, next) => {
    await post($, '/channel/lifecycle/session_end', { reason: e.reason })
    return next(e)
  })

  // Serves the tools registered above, and only those.
  on('tool.call', { tool: /^mcp__chargehand__/ }, async ($, e) => {
    // The engine's own keys are not the tool's input.
    const { tool, tool_use_id: _id, consent: _consent, ...input } = e as unknown as Reply
    return callTool($, typeof tool === 'string' ? tool : '', input)
  }).catch(() => ({ deny: UNREACHABLE }))
}
