// When is an open chat card over? Plain functions, tested in cards.test.mjs.

type CardTask = { id: number; status: string; mode?: string | null }
type CardButton = { data: string }

// The task a card is about: proposal (start:<id>), tidy card (park/close/done/
// delete/keep:<id>, merge:<id>:<into>) or a step of a started task. Cards
// without such a button (✅ steps: apply, deploy, archive) never go stale.
export const CARD_TASK = /^(?:start|park|close|done|delete|keep|merge|retry|replan|next|skip|opt|resume|restore):(\d+)/
const TIDY_CARD = /^(?:keep|delete|merge):/

function taskFinished(t: CardTask): string | null {
  if (t.status === 'done') return `#${t.id} schon erledigt`
  if (t.status === 'closed') return `#${t.id} geschlossen`
  if (t.status === 'backlog') return `#${t.id} im Backlog`
  return null
}

// Before a start tap: the task has gone somewhere else already.
export function taskMovedOn(t: CardTask): string | null {
  return taskFinished(t) ?? (t.mode === 'pipeline' ? `#${t.id} läuft bereits` : null)
}

// A card is over when none of its buttons is possible any more. A stopped
// task is a pipeline task too — its way-out buttons have to stay.
export function cardOutdated(r: { task: CardTask; actions: CardButton[] }, buttons: CardButton[]): string | null {
  if (buttons.some((b) => TIDY_CARD.test(b.data))) return taskFinished(r.task)
  const now = new Set(r.actions.map((a) => a.data))
  if (buttons.filter((b) => CARD_TASK.test(b.data)).some((b) => now.has(b.data))) return null
  return taskFinished(r.task) ?? 'überholt, aktueller Stand steht oben'
}
