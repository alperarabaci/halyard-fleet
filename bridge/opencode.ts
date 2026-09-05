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

export const HalyardGate = async ({ client, directory, worktree }: any) => {
  log("loaded", { directory, halyard: HALYARD })

  const answer = async (asked: Asked) => {
    const command = describe(asked)

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

  return {
    event: async ({ event }: { event: { type?: string; properties?: unknown } }) => {
      if (event?.type !== "permission.asked") return
      // Not awaited: this handler is called for every event opencode emits,
      // and holding it open for the length of an approval would stop the rest
      // of them being delivered.
      void answer(event.properties as Asked)
    },
  }
}

export default HalyardGate
