// The session list: projects, the tasks inside them, what is folded and what is pinned.
// No DOM here and only type imports: node runs the tests on this file as it is.

// a row of /api/sessions, the fields the list orders by
export type Row = { id: string; provider: string; at: number; isLive: boolean; task: string; group: string; groupName: string; epic?: string }
export type Task<R extends Row = Row> = { key: string; root: R; kids: R[]; at: number; pinned: boolean }
export type Project<R extends Row = Row> = { key: string; name: string; provider: string; tasks: Task<R>[]; at: number; pinned: boolean }
// folded: only what the viewer toggled, everything else is open; pinned: in the order it was pinned
export type Prefs = { folded: Record<string, boolean>; pinned: string[] }

const parse = (raw: string | null): unknown => {
  try { return raw ? JSON.parse(raw) : null } catch { return null }
}

// what the browser kept; anything odd there is dropped, not trusted
export function readPrefs(raw: { folded: string | null; pinned: string | null }): Prefs {
  const f = parse(raw.folded), p = parse(raw.pinned)
  const folded: Record<string, boolean> = {}
  if (f && typeof f === 'object' && !Array.isArray(f)) {
    for (const [k, v] of Object.entries(f)) if (typeof v === 'boolean') folded[k] = v
  }
  return { folded, pinned: Array.isArray(p) ? p.filter((k): k is string => typeof k === 'string') : [] }
}

// pinned first in the order they were pinned, then the freshest
function ordered<T extends { key: string; at: number }>(items: T[], pinned: string[]): Array<T & { pinned: boolean }> {
  const rank = (x: T) => { const i = pinned.indexOf(x.key); return i < 0 ? Infinity : i }
  return items.map(x => ({ ...x, pinned: pinned.includes(x.key) }))
    .sort((a, b) => rank(a) - rank(b) || b.at - a.at)
}

// a task is its root and the sessions it spawned; it stays in its root's project, wherever the kids work
export function projects<R extends Row>(rows: R[], prefs: Prefs): Project<R>[] {
  const ids = new Set(rows.map(s => s.id))
  const roots = rows.filter(s => !s.task || s.task === s.id || !ids.has(s.task))
  const byGroup = new Map<string, { name: string; provider: string; tasks: Array<Omit<Task<R>, 'pinned'>> }>()
  for (const root of roots) {
    const kids = rows.filter(s => s !== root && s.task === root.id).sort((a, b) => b.at - a.at)
    const at = Math.max(root.at, ...kids.map(k => k.at))
    const g = byGroup.get(root.group) ?? { name: root.groupName, provider: root.provider, tasks: [] }
    g.tasks.push({ key: root.id, root, kids, at })
    byGroup.set(root.group, g)
  }
  const list = [...byGroup].map(([group, g]) => ({
    key: 'p:' + group, name: g.name, provider: g.provider,
    tasks: ordered(g.tasks, prefs.pinned), at: Math.max(...g.tasks.map(t => t.at)),
  }))
  return ordered(list, prefs.pinned)
}

export const isFolded = (prefs: Prefs, key: string) => prefs.folded[key] ?? false

export const toggleFold = (prefs: Prefs, key: string): Prefs =>
  ({ ...prefs, folded: { ...prefs.folded, [key]: !isFolded(prefs, key) } })

export const togglePin = (prefs: Prefs, key: string): Prefs =>
  ({ ...prefs, pinned: prefs.pinned.includes(key) ? prefs.pinned.filter(k => k !== key) : [...prefs.pinned, key] })

// fold choices of groups no longer listed are dropped, or storage grows with every day's sessions
export const pruneFolded = (prefs: Prefs, keys: string[]): Prefs =>
  ({ ...prefs, folded: Object.fromEntries(Object.entries(prefs.folded).filter(([k]) => keys.includes(k))) })

// the office follows a task at its root: the root's office holds the whole task
export function followTarget(rows: Row[], id: string): string {
  const s = rows.find(r => r.id === id)
  return s && s.task && rows.some(r => r.id === s.task) ? s.task : id
}

// an epic has dozens of roles: the finished ones fold into «ещё N» until opened
export function visibleKids<R extends Row>(task: Task<R>, current: string, opened: boolean): { kids: R[]; more: number } {
  if (!task.root.epic || opened) return { kids: task.kids, more: 0 }
  const kids = task.kids.filter(k => k.isLive || k.id === current)
  return { kids, more: task.kids.length - kids.length }
}
