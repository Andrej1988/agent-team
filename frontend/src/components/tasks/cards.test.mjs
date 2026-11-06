import { test } from 'node:test'
import assert from 'node:assert/strict'
import { cardOutdated, taskMovedOn } from './cards.ts'

const stopped = { id: 158, status: 'blocked', mode: 'pipeline' }
const b = (...data) => data.map((d) => ({ data: d }))

test('the card of a stopped pipeline task keeps its buttons', () => {
  // regression: every such card said "läuft bereits" and hid its way out
  const actions = b('replan:158', 'done:158', 'close:158')
  assert.equal(cardOutdated({ task: stopped, actions }, b('replan:158', 'done:158', 'close:158')), null)
})

test('a card is over once none of its buttons is possible', () => {
  const r = { task: { ...stopped, status: 'running' }, actions: b('stop:158') }
  assert.match(cardOutdated(r, b('retry:158:2', 'replan:158')), /überholt/)
  assert.match(cardOutdated({ task: { ...stopped, status: 'done' }, actions: b('archive:158') }, b('done:158')), /schon erledigt/)
})

test('a proposal of a running task is over, of a waiting one not', () => {
  const r = { task: { id: 1, status: 'running', mode: 'pipeline' }, actions: b('stop:1') }
  assert.ok(cardOutdated(r, b('start:1', 'discuss:1', 'park:1')))
  assert.equal(cardOutdated({ task: { id: 1, status: 'new' }, actions: b('start:1', 'park:1') }, b('start:1', 'park:1')), null)
})

test('a tidy card stays until the task is finished', () => {
  assert.equal(cardOutdated({ task: stopped, actions: [] }, b('merge:158:7', 'keep:158')), null)
  assert.match(cardOutdated({ task: { ...stopped, status: 'closed' }, actions: [] }, b('keep:158')), /geschlossen/)
})

test('before a start tap a pipeline task counts as moved on', () => {
  assert.match(taskMovedOn(stopped), /läuft bereits/)
  assert.equal(taskMovedOn({ id: 2, status: 'new', mode: 'single' }), null)
})
