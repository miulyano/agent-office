// The session list: projects, the tasks inside them, what is folded and what is pinned.
// No DOM here and only type imports: node runs the tests on this file as it is.
const parse = (raw) => {
    try {
        return raw ? JSON.parse(raw) : null;
    }
    catch {
        return null;
    }
};
// what the browser kept; anything odd there is dropped, not trusted
export function readPrefs(raw) {
    const f = parse(raw.folded), p = parse(raw.pinned);
    const folded = {};
    if (f && typeof f === 'object' && !Array.isArray(f)) {
        for (const [k, v] of Object.entries(f))
            if (typeof v === 'boolean')
                folded[k] = v;
    }
    return { folded, pinned: Array.isArray(p) ? p.filter((k) => typeof k === 'string') : [] };
}
// pinned first in the order they were pinned, then the freshest
function ordered(items, pinned) {
    const rank = (x) => { const i = pinned.indexOf(x.key); return i < 0 ? Infinity : i; };
    return items.map(x => ({ ...x, pinned: pinned.includes(x.key) }))
        .sort((a, b) => rank(a) - rank(b) || b.at - a.at);
}
// a task is its root and the sessions it spawned; it stays in its root's project, wherever the kids work
export function projects(rows, prefs) {
    const ids = new Set(rows.map(s => s.id));
    const roots = rows.filter(s => !s.task || s.task === s.id || !ids.has(s.task));
    const byGroup = new Map();
    for (const root of roots) {
        const kids = rows.filter(s => s !== root && s.task === root.id).sort((a, b) => b.at - a.at);
        const at = Math.max(root.at, ...kids.map(k => k.at));
        const group = root.group ?? `${root.provider}:${root.project ?? ''}`;
        const g = byGroup.get(group) ?? { name: root.groupName ?? root.project ?? '', provider: root.provider, tasks: [] };
        g.tasks.push({ key: root.id, root, kids, at });
        byGroup.set(group, g);
    }
    const list = [...byGroup].map(([group, g]) => ({
        key: 'p:' + group, name: g.name, provider: g.provider,
        tasks: ordered(g.tasks, prefs.pinned), at: Math.max(...g.tasks.map(t => t.at)),
    }));
    return ordered(list, prefs.pinned);
}
export const isFolded = (prefs, key) => prefs.folded[key] ?? false;
export const toggleFold = (prefs, key) => ({ ...prefs, folded: { ...prefs.folded, [key]: !isFolded(prefs, key) } });
export const togglePin = (prefs, key) => ({ ...prefs, pinned: prefs.pinned.includes(key) ? prefs.pinned.filter(k => k !== key) : [...prefs.pinned, key] });
// fold choices of groups no longer listed are dropped, or storage grows with every day's sessions
export const pruneFolded = (prefs, keys) => ({ ...prefs, folded: Object.fromEntries(Object.entries(prefs.folded).filter(([k]) => keys.includes(k))) });
// the office follows a task at its root: the root's office holds the whole task
export function followTarget(rows, id) {
    const s = rows.find(r => r.id === id);
    return s && s.task && rows.some(r => r.id === s.task) ? s.task : id;
}
// an epic has dozens of roles: the finished ones fold into «ещё N» until opened
export function visibleKids(task, current, opened) {
    if (!task.root.epic || opened)
        return { kids: task.kids, more: 0 };
    const kids = task.kids.filter(k => k.isLive || k.id === current);
    return { kids, more: task.kids.length - kids.length };
}
