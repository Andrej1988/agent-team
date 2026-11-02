import { test } from 'node:test'
import assert from 'node:assert/strict'
import { taskHelp, errorHelp } from './taskHelp.ts'

const task = (patch = {}) => ({
  id: 101, status: 'blocked', mode: 'pipeline', round_count: 0,
  plan: { max_rounds: 5, max_stages: 20, rationale: '' }, ...patch,
})
const stage = (patch = {}) => ({
  task_id: 101, seq: 12, role: 'test-engineer', status: 'done', verdict: 'fail',
  findings: [], questions: [], ...patch,
})

test('Task 101 shows exhausted correction rounds and the current test failure', () => {
  const result = taskHelp(task({
    round_count: 5,
    stop_reason: 'exit criteria unmet, orchestrator proposed no further stages (the final completed stage is not a reviewer)',
  }), [stage({ findings: [{ id: 'TEST-3', resolved: false, text: 'A failed catalog refresh overwrites checked_at.' }] })])
  assert.equal(result.title, 'Korrekturlimit erreicht')
  assert.match(result.problem, /5 von 5/)
  assert.match(result.context, /Prüfzeitpunkt/)
  assert.match(result.input, /Entscheiden/)
  assert.match(result.next, /max rounds.*Save plan.*Re-plan/)
  assert.match(result.next, /Resume.*allein.*nicht/)
})

test('missing final review is a planning issue, not an instruction to raise limits', () => {
  const result = taskHelp(task({ stop_reason: 'exit criteria unmet, orchestrator proposed no further stages (the final completed stage is not a reviewer)' }), [])
  assert.equal(result.title, 'Abschlussprüfung fehlt im Plan')
  assert.match(result.input, /Keine fachliche Antwort/)
  assert.match(result.next, /Reviewer/)
  assert.doesNotMatch(result.next, /erhöhen|Resume/)
})

test('filesystem blockers ask for a permissions fix rather than more rounds', () => {
  const result = taskHelp(task({ stop_reason: 'stage 11 reported blocked: root:root files, cannot unlink, sudo unavailable' }), [])
  assert.equal(result.title, 'Dateirechte blockieren den Agenten')
  assert.match(result.next, /Limits ändern hilft hier nicht/)
})

test('real questions route to the answer field and keep the stage summary', () => {
  const result = taskHelp(task({ status: 'awaiting_input' }), [stage({ seq: 4, status: 'awaiting_input', summary: 'Live oder Demo?', questions: ['Welche Umgebung?'] })])
  assert.equal(result.target, 'answer')
  assert.equal(result.stageSeq, 4)
  assert.equal(result.problem, 'Live oder Demo?')
  assert.match(result.next, /Answer/)
})

test('a missing question is explicitly diagnosed rather than invented', () => {
  const result = taskHelp(task({ status: 'awaiting_input' }), [stage({ status: 'awaiting_input' })])
  assert.match(result.problem, /keine konkrete Frage/)
  assert.match(result.next, /Welche konkrete Information/)
})

test('budget requires an explicit cost decision', () => {
  const result = taskHelp(task({ budget_usd: 10, stop_reason: 'budget exceeded: $12.00 metered > $10.00' }), [])
  assert.equal(result.title, 'Kostenbudget erreicht')
  assert.match(result.problem, /\$10.00/)
  assert.match(result.input, /weitere Kosten/)
  assert.match(result.next, /angehalten lassen/)
})

test('stage limit requires a new plan after saving', () => {
  const result = taskHelp(task({ stop_reason: 'max_stages exceeded: plan would have 21 stages (limit 20)' }), [])
  assert.equal(result.title, 'Stufenlimit reicht nicht aus')
  assert.match(result.next, /Save plan.*Re-plan/)
})

test('explicit round limit does not require a reviewer phrase', () => {
  assert.equal(taskHelp(task({ stop_reason: 'max_rounds exceeded: 6 failed stages > 5', round_count: 6 }), []).title, 'Korrekturlimit erreicht')
})

test('repeated findings need an investigation, not blind retries', () => {
  const result = taskHelp(task({ stop_reason: 'test-engineer cited the same unresolved finding(s) twice: TEST-3' }), [])
  assert.equal(result.title, 'Derselbe Fehler besteht weiterhin')
  assert.match(result.next, /gezielten Korrekturschritt/)
})

test('a manual stop gives resume instructions', () => {
  const result = taskHelp(task({ stop_reason: 'stopped by user' }), [])
  assert.equal(result.title, 'Pipeline manuell gestoppt')
  assert.match(result.next, /Resume.*Run next/)
})

test('provider caps do not guess Anthropic or a monthly reset', () => {
  const result = taskHelp(task({ status: 'limit_reached', error: 'Codex capacity exhausted' }), [])
  assert.equal(result.title, 'KI-Anbieter begrenzt den Lauf')
  assert.equal(result.target, 'plan')
  assert.doesNotMatch(JSON.stringify(result), /Anthropic|monatlich|monthly/)
})

test('failed stage points at its own retry control', () => {
  const result = taskHelp(task({ status: 'failed', error: 'agent run ended in error (subtype=success)' }), [stage({ seq: 8, status: 'failed', error: 'same generic error' })])
  assert.equal(result.stageSeq, 8)
  assert.match(result.next, /Stufe 8.*Retry/)
  assert.equal(result.details, 'agent run ended in error (subtype=success)')
  assert.doesNotMatch(result.title, /Anmeldung/)
})

test('explicit OAuth error routes to existing login UI', () => {
  assert.equal(taskHelp(task({ status: 'failed', error: 'Claude-Anmeldung abgelaufen oder ungültig.' }), []).target, 'login')
})

test('simple failures do not show pipeline-only controls', () => {
  const result = taskHelp(task({ status: 'failed', mode: 'simple', error: 'process exited unexpectedly' }), [])
  assert.equal(result.target, 'run')
  assert.doesNotMatch(result.next, /Stufe|Re-plan|Resume/)
})

test('credential instructions keep secrets out of task text', () => {
  const result = errorHelp('credentials missing')
  assert.equal(result.title, 'Zugangsdaten fehlen')
  assert.match(result.next, /Keine Schlüssel/)
})

test('unknown blockers keep evidence and do not invent a question', () => {
  const result = taskHelp(task({ stop_reason: 'custom blocker' }), [stage({ summary: 'External approval still pending.' })])
  assert.equal(result.problem, 'External approval still pending.')
  assert.match(result.input, /keine konkrete Rückfrage/)
  assert.match(result.details, /custom blocker/)
})

test('resolved historical findings do not replace the current failure', () => {
  const result = taskHelp(task({ stop_reason: 'custom blocker' }), [stage({ seq: 1, findings: [{ id: 'OLD', resolved: false, text: 'old' }] }), stage({ seq: 2, findings: [{ id: 'NEW', resolved: true, text: 'fixed' }], summary: 'current summary' })])
  assert.equal(result.context, 'current summary')
})

test('stale stage data from another selected task is ignored', () => {
  const result = taskHelp(task({ status: 'awaiting_input' }), [stage({ task_id: 999, status: 'awaiting_input', summary: 'Wrong task question' })])
  assert.equal(result.title, 'Rückfrage noch nicht verfügbar')
})

test('ready pipelines explain the pending manual start', () => {
  const result = taskHelp(task({ status: 'plan_ready' }), [stage({ status: 'pending' })])
  assert.match(result.next, /Run next/)
  assert.equal(result.context, undefined)
})

test('healthy active and completed tasks have no attention warning', () => {
  for (const status of ['new', 'running', 'planning', 'refining', 'executing', 'done', 'refined', 'backlog']) {
    assert.equal(taskHelp(task({ status }), []), null)
  }
})

test('API and network errors retain raw details but explain the next step', () => {
  for (const error of ['409 Conflict', '422 invalid max_rounds', 'Failed to fetch', '503 worker unavailable']) {
    const result = errorHelp(error)
    assert.equal(result.details, error)
    assert.notEqual(result.title, 'Aktion fehlgeschlagen')
    assert.ok(result.input && result.next)
  }
})

test('malformed orchestrator results require replan rather than a user answer', () => {
  const result = errorHelp('Orchestrator produced no parseable # Plan JSON block', true)
  assert.equal(result.target, 'plan')
  assert.match(result.input, /Keine fachliche Antwort/)
})
