// Логика списка сессий: проекты, задачи, свёрнутые и закреплённые. Запуск: node --experimental-strip-types --test tests/list.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readPrefs, projects, isFolded, toggleFold, togglePin, pruneFolded, followTarget, visibleKids } from '../src/list.ts'

// сессия за сутки, как её отдаёт /api/sessions
const row = (id, at, more = {}) => ({ id, at, provider: 'claude', isLive: false, task: id, group: 'claude:/work/app', groupName: 'app', ...more })
const none = { folded: {}, pinned: [] }
const ids = list => list.map(p => [p.key, p.tasks.map(t => [t.root.id, t.kids.map(k => k.id)])])

test('sessions group by project, Claude and Codex apart', () => {
  const list = projects([
    row('a', 3),
    row('x', 2, { provider: 'codex', group: 'codex:/work/app' }),
    row('b', 1),
  ], none)
  assert.deepEqual(ids(list), [['p:claude:/work/app', [['a', []], ['b', []]]], ['p:codex:/work/app', [['x', []]]]])
  assert.deepEqual(list.map(p => [p.name, p.provider]), [['app', 'claude'], ['app', 'codex']])
})

test('a row from a server without projects groups by provider and folder', () => {
  const old = (id, at, provider, project) => ({ id, at, provider, project, isLive: false, task: id })
  const list = projects([old('a', 3, 'claude', 'app'), old('x', 2, 'codex', 'app'), old('b', 1, 'claude', 'site')], none)
  assert.deepEqual(list.map(p => [p.key, p.name, p.provider]), [['p:claude:app', 'app', 'claude'], ['p:codex:app', 'app', 'codex'], ['p:claude:site', 'site', 'claude']])
})

test('spawned sessions stay under their root inside the project', () => {
  const list = projects([row('kid', 5, { task: 'root' }), row('root', 1), row('other', 3)], none)
  assert.deepEqual(ids(list), [['p:claude:/work/app', [['root', ['kid']], ['other', []]]]])
})

test('a kid in another folder stays with its root', () => {
  const list = projects([row('root', 1), row('kid', 2, { task: 'root', group: 'claude:/work/site', groupName: 'site' })], none)
  assert.deepEqual(ids(list), [['p:claude:/work/app', [['root', ['kid']]]]])
})

test('inside a level the freshest goes first', () => {
  const list = projects([
    row('old', 1, { group: 'claude:/work/a' }),
    row('new', 9, { group: 'claude:/work/b' }),
    row('mid', 5, { group: 'claude:/work/a' }),
  ], none)
  assert.deepEqual(ids(list).map(([k]) => k), ['p:claude:/work/b', 'p:claude:/work/a'])
  assert.deepEqual(ids(list)[1][1].map(([id]) => id), ['mid', 'old'])
})

test('pinned projects lead the list in pin order', () => {
  const rows = [row('a', 9, { group: 'claude:/a' }), row('b', 5, { group: 'claude:/b' }), row('c', 1, { group: 'claude:/c' })]
  const list = projects(rows, { folded: {}, pinned: ['p:claude:/c', 'p:claude:/b'] })
  assert.deepEqual(list.map(p => [p.key, p.pinned]), [['p:claude:/c', true], ['p:claude:/b', true], ['p:claude:/a', false]])
})

test('a pinned session leads its project', () => {
  const list = projects([row('a', 9), row('b', 5), row('c', 1)], { folded: {}, pinned: ['c'] })
  assert.deepEqual(list[0].tasks.map(t => [t.key, t.pinned]), [['c', true], ['a', false], ['b', false]])
})

test('an unpinned item goes back to its place by freshness, the others keep their order', () => {
  const rows = [row('a', 9), row('b', 5), row('c', 1)]
  let prefs = { folded: {}, pinned: ['c', 'b'] }
  prefs = togglePin(prefs, 'c')
  assert.deepEqual(prefs.pinned, ['b'])
  assert.deepEqual(projects(rows, prefs)[0].tasks.map(t => t.key), ['b', 'a', 'c'])
  prefs = togglePin(prefs, 'a')
  assert.deepEqual(prefs.pinned, ['b', 'a'])
})

test('a pinned key absent today stays in storage', () => {
  const prefs = { folded: {}, pinned: ['gone', 'a'] }
  assert.deepEqual(projects([row('a', 1)], prefs)[0].tasks.map(t => t.key), ['a'])
  assert.deepEqual(togglePin(prefs, 'a').pinned, ['gone'])
})

test('projects and tasks are open by default, a stored choice wins', () => {
  assert.equal(isFolded(none, 'p:claude:/work/app'), false)
  const prefs = toggleFold(none, 'p:claude:/work/app')
  assert.equal(isFolded(prefs, 'p:claude:/work/app'), true)
  assert.equal(isFolded(toggleFold(prefs, 'p:claude:/work/app'), 'p:claude:/work/app'), false)
  assert.deepEqual(none, { folded: {}, pinned: [] })   // prefs are never changed in place
})

test('folded keys of groups gone from the list are pruned', () => {
  const prefs = { folded: { 'p:claude:/a': true, gone: true, root: false }, pinned: ['gone'] }
  assert.deepEqual(pruneFolded(prefs, ['p:claude:/a', 'root']), { folded: { 'p:claude:/a': true, root: false }, pinned: ['gone'] })
})

test('broken storage falls back to defaults', () => {
  assert.deepEqual(readPrefs({ folded: null, pinned: null }), none)
  assert.deepEqual(readPrefs({ folded: '{не json', pinned: '5' }), none)
  assert.deepEqual(readPrefs({ folded: '["a"]', pinned: '{"a": 1}' }), none)
  assert.deepEqual(readPrefs({ folded: '{"a": true, "b": "x"}', pinned: '["a", 3, "b"]' }), { folded: { a: true }, pinned: ['a', 'b'] })
})

test('auto-follow of a spawned session lands on its root, a root stays itself', () => {
  const rows = [row('root', 1), row('kid', 2, { task: 'root' }), row('orphan', 3, { task: 'gone' })]
  assert.equal(followTarget(rows, 'kid'), 'root')
  assert.equal(followTarget(rows, 'root'), 'root')
  assert.equal(followTarget(rows, 'orphan'), 'orphan')
})

test('finished roles of an epic fold into «ещё N», the current one stays', () => {
  const [task] = projects([
    row('coord', 9, { epic: 'e1' }),
    row('w1', 8, { task: 'coord', isLive: true }),
    row('q1', 7, { task: 'coord' }),
    row('q2', 6, { task: 'coord' }),
  ], none)[0].tasks
  assert.deepEqual(visibleKids(task, '', false), { kids: [task.kids[0]], more: 2 })
  assert.deepEqual(visibleKids(task, 'q2', false).kids.map(k => k.id), ['w1', 'q2'])
  assert.deepEqual(visibleKids(task, '', true), { kids: task.kids, more: 0 })
  // an ordinary task shows all its kids
  const [plain] = projects([row('r', 2), row('k', 1, { task: 'r' })], none)[0].tasks
  assert.deepEqual(visibleKids(plain, '', false), { kids: plain.kids, more: 0 })
})
