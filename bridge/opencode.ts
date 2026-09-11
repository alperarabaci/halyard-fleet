/**
 * The gate, for opencode.
 *
 * The other four bridge scripts are Python, stdlib-only, because they run from
 * a hook with no virtualenv and nothing importable. This one is TypeScript for
 * the same kind of reason: opencode's extension point is a plugin module, so a
 * Python script could not be loaded at all. Its equivalent of "import nothing"
 * is that it uses only what Bun already has — `fetch` and `node:*` — and pulls
 * in no package, so `.opencode/plugins/` never needs an install.
 *
 * **The permission hook does not fire.** `@opencode-ai/plugin` 1.18.29 declares
 * `permission.ask`, with a signature that reads exactly like a gate: an input
 * and a mutable `{ status }`. It was measured on opencode 1.18.29 against a
 * real approval, twice, and it was never called. What arrives instead is a
 * `permission.asked` *event*, and the answer goes back over the HTTP API. So
 * the gate here is not a function that returns a verdict; it is something that
 * hears a question and sends an answer.
 *
 * That difference is worth stating plainly, because it changes what happens
 * when Halyard is unreachable. Elsewhere a bridge that cannot get an answer
 * denies — the runtime is blocked waiting on it, and an unanswered gate that
 * lets the command through is not a gate. Here nobody is waiting on this code:
 * opencode has already put the question on the screen and is waiting for
 * *anyone* to answer it. Saying nothing leaves it there for whoever is at the
 * desk. So this fails to the desk rather than to a refusal, and that is the
 * safer of the two — nothing runs unapproved either way, and the difference is
 * only whether a person can still say yes.
 *
 * The same property makes the phone and the desk equals: both can answer, and
 * the first one wins. That is the arrangement somebody working at the machine
 * actually wants.
 *
 * **`allow` is answered with `once`, never `always`.** opencode offers to
 * remember a pattern — the event carries the `always` it would save. Taking it
 * would move the decision out of Halyard and into opencode's own saved list,
 * where nothing is audited and `/pause` does not reach. What may go through
 * without a person is configured in `halyard.yaml` and written to the audit log
 * with the pattern that allowed it; that is the only place it should live.
 */

import { appendFileSync } from "node:fs"

/** Where the control plane listens. `HALYARD_BIND` in `halyard.yaml`. */
const HALYARD = process.env.HALYARD_URL ?? "http://127.0.0.1:8799"

/**
 * How long to wait for an answer.
 *
 * Bounded, but not because opencode needs it back: it waits indefinitely for
 * the screen. It is bounded so this plugin does not hold a promise open for a
 * question somebody already answered at the desk.
 */
const TIMEOUT_MS = Number(process.env.HALYARD_TIMEOUT_MS ?? 300_000)

/** Set to a path to record what this decided. Off unless asked for. */
const LOG = process.env.HALYARD_OPENCODE_LOG

const log = (what: string, detail: Record<string, unknown> = {}) => {
  if (!LOG) return
  try {
    appendFileSync(LOG, JSON.stringify({ at: new Date().toISOString(), what, ...detail }) + "\n")
  } catch {
    // A gate that breaks over its own logging would be worse than a quiet one.
  }
}

/**
 * What a failed turn looks like. Measured, on a real one:
 *
 *     { name: "APIError", data: {
 *         message: "Usage limit reached for 5 hour. Your limit will reset at …",
 *         statusCode: 429, isRetryable: true,
 *         metadata: { url: "https://api.z.ai/api/coding/paas/v4/chat/completions" } } }
 *
 * The same event carries `MessageAbortedError` when somebody presses escape,
 * which is why this is not simply "relay session errors": a phone that buzzed
 * every time a turn was interrupted at the desk would be muted within a day.
 */
type Failed = {
  sessionID?: string
  error?: {
    name?: string
    data?: {
      message?: string
      statusCode?: number
      metadata?: { url?: string }
    }
  }
}

type Asked = {
  id: string
  sessionID: string
  /** The category opencode gates on: `bash`, `edit`, `webfetch`, … */
  permission?: string
  patterns?: string[]
  metadata?: { command?: string; [key: string]: unknown }
  always?: string[]
  tool?: { messageID?: string; callID?: string }
}

/**
 * What the card is about.
 *
 * `metadata.command` is what a person needs to see for a shell call. The
 * other categories carry no command, and `patterns` is what opencode itself
 * shows — measured on a real `bash` ask, both were present and equal.
 */
const describe = (asked: Asked) =>
  asked.metadata?.command ?? asked.patterns?.[0] ?? asked.permission ?? "(no command given)"

/**
 * The question in the words opencode puts on its own screen, where it is not
 * simply "may this command run".
 *
 * The event carries no title. Read out of 1.18.29's own source, `permission.asked`
 * is `{id, sessionID, permission, patterns, metadata, always, tool}`, and the TUI
 * composes its heading from those. Only `external_directory` is copied, because
 * that is the rule that was read: the directory from `metadata.parentDir`, then
 * `metadata.filepath`, then the first pattern cut at its wildcard, with home
 * shortened to `~`. Anything else returns nothing and the card stays as it was —
 * guessing the TUI's wording would put a sentence on a phone that the screen
 * never showed.
 */
const asksAbout = (asked: Asked): string | undefined => {
  if (asked.permission !== "external_directory") return undefined
  const text = (value: unknown) => (typeof value === "string" && value ? value : undefined)
  const first = text(asked.patterns?.[0])
  const cut = first?.includes("*") ? first.slice(0, first.indexOf("*")).replace(/[\\/]+$/, "") : first
  const where = text(asked.metadata?.parentDir) ?? text(asked.metadata?.filepath) ?? text(cut)
  if (!where) return undefined
  const home = process.env.HOME
  const shown = home && where.startsWith(home) ? `~${where.slice(home.length)}` : where
  return `Access external directory ${shown}`
}

export const HalyardGate = async ({ client, directory, worktree }: any) => {
  log("loaded", { directory, halyard: HALYARD })

  /** The last reply relayed per session, so one turn is not sent twice. */
  const relayed = new Map<string, string>()

  const answer = async (asked: Asked) => {
    const command = describe(asked)
    const asks = asksAbout(asked)

    let decision: string | undefined
    try {
      const asking = await fetch(`${HALYARD}/v1/approvals`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          session_id: asked.sessionID,
          agent_id: "opencode",
          tool: asked.permission ?? "bash",
          command,
          tool_use_id: asked.tool?.callID,
          cwd: directory,
          project_dir: worktree ?? directory,
          // Only where the question is not simply the command: for a shell call
          // the pattern *is* the command, and showing it twice is noise.
          ...(asks ? { asks, patterns: asked.patterns } : {}),
        }),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      })
      if (!asking.ok) throw new Error(`control plane answered ${asking.status}`)
      decision = ((await asking.json()) as { decision?: string }).decision
    } catch (unreachable) {
      // Deliberately nothing. The question is on the screen; leaving it there
      // is the whole fallback. See the note at the top of this file.
      log("left to the desk", { id: asked.id, why: String(unreachable) })
      return
    }

    // Only an exact allow allows, and it allows once. A missing field, a typo
    // or a null is not an approval — and `defer` means the gate is paused,
    // which here means the same as not answering: the desk decides.
    const response = decision === "allow" ? "once" : decision === "deny" ? "reject" : undefined
    if (!response) {
      log("not answered", { id: asked.id, decision })
      return
    }

    try {
      await client.postSessionIdPermissionsPermissionId({
        path: { id: asked.sessionID, permissionID: asked.id },
        body: { response },
      })
      log("answered", { id: asked.id, decision, response })
    } catch (refused) {
      // Someone at the desk answering first lands here, and is not a fault.
      log("could not answer", { id: asked.id, why: String(refused) })
    }
  }

  /**
   * A turn that stopped for a reason worth knowing about, on the phone.
   *
   * Nothing reports this otherwise. The session simply goes quiet, and the
   * person who started it goes on believing work is happening — which is how
   * an evening was spent waiting on a run that had stopped in its first
   * minute.
   *
   * Only what a person can act on. `statusCode` is the test rather than the
   * wording: a provider that rephrases its message should not silence this,
   * and 429 means the same thing at every one of them. An interrupt at the
   * desk arrives on this same event and is not news to anybody.
   */
  const report = async (failure: Failed) => {
    const code = failure.error?.data?.statusCode
    const said = failure.error?.data?.message
    if (!said || typeof code !== "number" || code < 400) return

    // The provider, from the endpoint it was talking to — "api.z.ai" says more
    // about which limit was reached than any name this file could invent.
    let host = ""
    try {
      host = new URL(failure.error?.data?.metadata?.url ?? "").host
    } catch {
      host = ""
    }
    const text = code === 429 ? `⛔️ ${said}${host ? ` (${host})` : ""}` : `⚠️ ${said} [${code}]`

    try {
      await fetch(`${HALYARD}/v1/messages`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          session_id: failure.sessionID,
          agent_id: "opencode",
          text,
          cwd: directory,
          project_dir: worktree ?? directory,
        }),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      })
      log("reported", { code, host })
    } catch (unreachable) {
      log("could not report", { code, why: String(unreachable) })
    }
  }

  /**
   * What the agent said, once it has stopped saying it.
   *
   * The other runtimes have a hook for this — the one that fires when a turn
   * ends — and relaying it is what makes a phone a place to read replies
   * rather than only to approve things. opencode has no such hook; it has an
   * event, `session.idle`, and the text has to be fetched afterwards.
   *
   * Fetched rather than accumulated. A turn emits `message.updated` for every
   * part as it is written, and assembling the reply from those would mean
   * keeping half-written state in a plugin that can be reloaded mid-turn. The
   * finished message is already stored; asking for it once is simpler and
   * cannot drift.
   *
   * `session.idle` fires more than once for one turn — measured, twice in the
   * same second — so the last message relayed is remembered per session. That
   * is also what stops a reply being sent twice when a turn ends, is compacted,
   * and settles again.
   */
  const relay = async (sessionID: string) => {
    if (!sessionID) return
    let messages: any[]
    try {
      const got = await client.session.messages({
        path: { id: sessionID },
        query: { directory, limit: 1 },
      })
      messages = (got as any)?.data ?? got ?? []
    } catch (unreadable) {
      log("could not read the reply", { sessionID, why: String(unreadable) })
      return
    }

    const last = Array.isArray(messages) ? messages[messages.length - 1] : undefined
    const info = last?.info ?? {}
    // Only what the agent said. A user message is the thing somebody typed a
    // moment ago, and sending it back to them is noise.
    if (info?.role !== "assistant" || !info?.id) return
    if (relayed.get(sessionID) === info.id) return

    const text = (last?.parts ?? [])
      .filter((part: any) => part?.type === "text" && !part?.synthetic && part?.text)
      .map((part: any) => String(part.text))
      .join("\n")
      .trim()
    if (!text) return

    relayed.set(sessionID, info.id)
    try {
      await fetch(`${HALYARD}/v1/messages`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          session_id: sessionID,
          agent_id: "opencode",
          text,
          cwd: directory,
          project_dir: worktree ?? directory,
        }),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      })
      log("relayed", { sessionID, messageID: info.id, length: text.length })
    } catch (unreachable) {
      // Put back, so the next idle tries again rather than deciding this reply
      // has already been delivered.
      relayed.delete(sessionID)
      log("could not relay", { sessionID, why: String(unreachable) })
    }
  }

  return {
    event: async ({ event }: { event: { type?: string; properties?: unknown } }) => {
      // Not awaited, either of them: this handler is called for every event
      // opencode emits, and holding it open for the length of an approval
      // would stop the rest of them being delivered.
      if (event?.type === "permission.asked") {
        void answer(event.properties as Asked)
        return
      }
      if (event?.type === "session.error") {
        void report(event.properties as Failed)
        return
      }
      if (event?.type === "session.idle") {
        void relay(String((event.properties as any)?.sessionID ?? ""))
      }
    },
  }
}

export default HalyardGate
