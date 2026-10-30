// Menu bar popover: sessions and spend stats, in the office's palette.
// Lives on the server like the office itself, so a style change shows up the
// next time the popover opens, without rebuilding the app.
import { money } from './office.js';
const bridge = window.webkit?.messageHandlers?.ao;
const $ = (id) => document.getElementById(id);
const esc = (s) => s.replace(/[&<>"]/g, c => `&${{ '&': 'amp', '<': 'lt', '>': 'gt', '"': 'quot' }[c]};`);
let sessions = [];
let stats = null;
let online = false;
let tab = load('tab', 'sessions');
let range = load('range', '30');
let provider = load('provider', 'all'); // all, claude или codex: один фильтр на статистику и токены
let autostart = false;
let active = true;
function load(key, fallback) {
    try {
        return localStorage.getItem('ao.' + key) ?? fallback;
    }
    catch {
        return fallback;
    }
}
function keep(key, value) {
    try {
        localStorage.setItem('ao.' + key, value);
    }
    catch { /* private mode */ }
}
// ── Numbers ──
function pct(part, all) {
    const p = all ? (part / all) * 100 : 0;
    // a sliver is not «0%»: model output is well under a percent of all tokens
    return p > 0 && p < 0.1 ? '<0.1%' : p > 0 && p < 1 ? p.toFixed(1) + '%' : Math.round(p) + '%';
}
function tokens(n) {
    if (n >= 1e9)
        return (n / 1e9).toFixed(1).replace(/\.0$/, '') + ' млрд';
    if (n >= 1e6)
        return Math.round(n / 1e6) + ' млн';
    if (n >= 1e3)
        return Math.round(n / 1e3) + ' тыс';
    return String(Math.round(n));
}
const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const parse = (day) => new Date(day + 'T00:00:00');
const short = (day) => { const d = parse(day); return `${d.getDate()} ${MONTHS[d.getMonth()]}`; };
const weekday = (day) => (parse(day).getDay() + 6) % 7;
function ago(t) {
    const m = Math.round((Date.now() - t) / 60000);
    return m < 60 ? `${m} мин назад` : `${Math.round(m / 60)} ч назад`;
}
// ── Models: fixed order, fixed colors (validated against the dark panel) ──
const MODELS = ['fable', 'opus', 'sonnet', 'haiku', 'astra', 'sol', 'terra', 'luna', 'gpt', 'other'];
const family = (model) => MODELS.find(f => model.includes(f)) ?? 'other';
const color = (f) => `var(--m-${f})`;
function modelName(model) {
    if (model === 'codex-auto-review')
        return 'Авторевью';
    if (model.startsWith('gpt-')) {
        const [, version, ...rest] = model.split('-');
        return ['GPT-' + version, ...rest.map(w => w[0]?.toUpperCase() + w.slice(1))].join(' ');
    }
    const m = model.replace(/^claude-/, '').split('-');
    const name = m[0] ?? model;
    return name[0]?.toUpperCase() + name.slice(1) + (m.length > 1 ? ' ' + m.slice(1).join('.') : '');
}
// several models in one line don't fit the name column: the family's name, the list in the tooltip
function familyRow(f, slot) {
    const names = [...slot.names];
    const name = names.length === 1 ? names[0] : f === 'gpt' ? 'GPT старые' : f[0].toUpperCase() + f.slice(1);
    return { name, usd: slot.usd, fill: color(f), tip: `<b>${esc(name)}</b><br>${esc(names.join(', '))}` };
}
function byFamily(days, of = d => d.models) {
    const out = new Map();
    for (const d of days) {
        for (const [model, usd] of Object.entries(of(d))) {
            const f = family(model);
            const slot = out.get(f) ?? { usd: 0, names: new Set() };
            slot.usd += usd;
            slot.names.add(modelName(model));
            out.set(f, slot);
        }
    }
    return out;
}
// ── Pixel building: the same office as the menu bar and app icons ──
const LOGO = [
    '................',
    '.....GGGGGG.....',
    '.....GGGGGG.....',
    '..RRRRRRRRRRRR..',
    '..BBBBBBBBBBBB..',
    '..BBCCBCCBYYBB..',
    '..BBCCBCCBYYBB..',
    '..BBBBBBBBBBBB..',
    '..BBDDBCCBCCBB..',
    '..BBDDBCCBCCBB..',
    '..BBBBBBBBBBBB..',
    '..BBCCBOOBCCBB..',
    '..BBCCBOOBCCBB..',
    '..BBBBBOOBBBBB..',
    'LLLLLLLLLLLLLLLL',
    '................',
];
const LOGO_COLORS = {
    G: '#f0c040', R: '#8a4a5a', B: '#4b5a8a', C: '#6ee7ff', Y: '#f0c040', D: '#1b1e2b', O: '#a0673d', L: '#3a3e63',
};
function logo() {
    let s = '';
    LOGO.forEach((row, y) => [...row].forEach((ch, x) => {
        const fill = LOGO_COLORS[ch];
        if (fill)
            s += `<rect x="${x}" y="${y}" width="1" height="1" fill="${fill}"/>`;
    }));
    return s;
}
// ── Talking to the app ──
function send(cmd, arg) {
    if (bridge)
        bridge.postMessage({ cmd, arg });
}
// Height of the content itself: documentElement.scrollHeight never drops below the window,
// so after the long stats tab the popover would stay tall on the short sessions tab.
function fit() {
    send('size', Math.ceil(document.body.getBoundingClientRect().height));
}
function openPath(path) {
    if (bridge)
        send('open', path);
    else
        window.open(path, '_blank');
}
async function get(path) {
    try {
        const res = await fetch(path, { cache: 'no-store' });
        return res.ok ? await res.json() : null;
    }
    catch {
        return null;
    }
}
async function refresh(withStats = false) {
    const list = await get('api/sessions');
    online = list !== null;
    if (list)
        sessions = list;
    if (withStats || !stats || tab !== 'sessions') {
        const asked = provider;
        const fresh = await get(asked === 'all' ? 'api/stats' : 'api/stats?p=' + asked);
        if (asked === provider)
            stats = fresh ?? stats;
    }
    render();
}
// ── Header ──
function renderState() {
    const busy = sessions.filter(s => s.working > 0).length;
    $('state').innerHTML = !online
        ? '<span class="lamp idle"></span>сервер не отвечает'
        : busy
            ? `<span class="lamp"></span>в работе: ${busy}`
            : '<span class="lamp idle"></span>все отдыхают';
}
// ── Sessions tab ──
function sessionRow(s, lamp) {
    const state = s.working > 0 ? `в работе: ${s.working}` : s.isLive ? `ждёт · ${ago(s.at)}` : ago(s.at);
    // главное — сегодняшнее; полная цена сессии, если она шла и раньше
    const money2 = !s.today
        ? `<div class="a">${money(s.cost)}</div><div class="b">до сегодня</div>`
        : money(s.today) === money(s.cost)
            ? `<div class="a">${money(s.today)}</div><div class="b">сегодня</div>`
            : `<div class="a">${money(s.today)}</div><div class="b">сегодня · всего ${money(s.cost)}</div>`;
    return `<button class="row p-${s.provider}" data-open="/#${esc(s.id)}" title="Открыть офис сессии">
    ${lamp ? `<span class="lamp${s.working > 0 ? '' : ' idle'}"></span>` : ''}
    <span class="body"><div class="t">${esc(s.title)}</div><div class="p"><em>${s.provider === 'codex' ? 'CODEX' : 'CLAUDE'}</em>${esc(s.project)} · ${state}</div></span>
    <span class="money">${money2}</span></button>`;
}
// who spent today: shown once there is something to compare
function splitBar() {
    const of = (p) => sessions.filter(s => s.provider === p).reduce((a, s) => a + (s.today ?? 0), 0);
    const claude = of('claude'), codex = of('codex'), all = claude + codex;
    if (!claude || !codex)
        return '';
    return `<div class="split"><i style="width:${(claude / all) * 100}%;background:var(--claude)"></i><i style="width:${(codex / all) * 100}%;background:var(--codex)"></i></div>
    <div class="splits"><span><i style="background:var(--claude)"></i>Claude Code <b>${money(claude)}</b></span><span><i style="background:var(--codex)"></i>Codex <b>${money(codex)}</b></span></div>`;
}
function renderSessions() {
    const today = sessions.reduce((a, s) => a + (s.today ?? 0), 0);
    const busy = sessions.filter(s => s.working > 0);
    const hands = busy.reduce((a, s) => a + s.working, 0);
    // сутки календарные: с 00:00 до 00:00, вчерашний вечер сюда не входит
    const midnight = new Date().setHours(0, 0, 0, 0);
    let html = `<div class="tiles two">
    <div class="tile pix"><div class="k">Сегодня</div><div class="v">${money(today)}</div><div class="s">с 00:00</div></div>
    <div class="tile pix"><div class="k">В работе</div><div class="v">${busy.length ? '<span class="lamp big"></span>' : ''}${busy.length}</div><div class="s">${busy.length ? `исполнителей: ${hands}` : 'сессий'}</div></div>
  </div>`;
    html += splitBar();
    const live = sessions.filter(s => s.isLive).sort((a, b) => b.working - a.working || b.at - a.at);
    const rest = sessions.filter(s => !s.isLive && ((s.today ?? 0) > 0 || s.at >= midnight));
    html += `<section><h3>АКТИВНЫЕ <span class="hint">${live.length || ''}</span></h3>`;
    html += live.length ? live.map(s => sessionRow(s, true)).join('') : `<div class="empty">${online ? 'Активных сессий нет' : 'Сервер не отвечает'}</div>`;
    html += '</section>';
    if (rest.length) {
        html += `<details${load('rest', '') ? ' open' : ''} id="rest"><summary><h3><span>РАНЬШЕ СЕГОДНЯ</span> <span class="hint">${rest.length} · ${money(rest.reduce((a, s) => a + (s.today ?? 0), 0))}</span></h3></summary>`;
        html += rest.map(s => sessionRow(s, false)).join('') + '</details>';
    }
    return html;
}
// ── Stats tab ──
const W = 348;
const PROVIDERS = [['claude', 'Claude'], ['codex', 'Codex']];
// One quiet row under the tabs: the period on the left, who on the right.
// Providers work like a chart legend: both lit is everything, a click leaves one, another click brings both back.
function filters(all) {
    const who = PROVIDERS.map(([k, name]) => `<button data-provider="${k}" class="p-${k}${provider !== 'all' && provider !== k ? ' off' : ''}"
    title="${provider === k ? 'Показать всех' : 'Только ' + name}"><i></i>${name}</button>`).join('');
    return `<div class="filters"><span class="ranges">${ranges(all)
        .map(([k, t]) => `<button data-range="${k}" class="${range === k ? 'on' : ''}">${t}</button>`).join('')}</span><span class="who">${who}</span></div>`;
}
// the two histories start apart: an empty early stretch does not mean nobody worked
function historyNote() {
    const first = stats?.first ?? {};
    const parts = [first.claude && `Claude Code с ${short(first.claude)}`, first.codex && `Codex с ${short(first.codex)}`].filter(Boolean);
    return `<p class="note">История: ${parts.join(', ')}. Сервер копит её в своей папке, поэтому она переживает чистку журналов. Суммы посчитаны по ценам API, по подписке счёт другой. Обычные чаты claude.ai и ChatGPT сюда не входят.</p>`;
}
function until(seconds) {
    const h = (seconds * 1000 - Date.now()) / 3600e3;
    return h >= 24 ? `${Math.round(h / 24)} дн` : h >= 1 ? `${Math.round(h)} ч` : `${Math.max(1, Math.round(h * 60))} мин`;
}
// subscription limits come from Codex's own logs; a window past its reset has started over
function renderLimits() {
    const limits = stats?.limits;
    if (provider === 'claude' || !limits?.windows?.length)
        return '';
    const rows = limits.windows.map(w => {
        const over = w.resets * 1000 < Date.now();
        const used = over ? 0 : w.used;
        const name = w.minutes >= 10080 ? 'Неделя' : w.minutes >= 1440 ? `${Math.round(w.minutes / 1440)} дн` : `${Math.round(w.minutes / 60)} ч`;
        return `<div class="line"><span class="name">${name}</span><span class="track"><span class="fill" style="display:block;width:${Math.min(used, 100)}%;background:var(--codex)"></span></span>
      <span class="val">${Math.round(used)}% <small>${over ? 'сброшен' : until(w.resets)}</small></span></div>`;
    }).join('');
    return `<section><h3>ЛИМИТЫ CODEX <span class="hint">${limits.plan ? esc(limits.plan) + ' · ' : ''}занято и до сброса</span></h3><div class="bars">${rows}</div></section>`;
}
function rangeDays(all) {
    return range === 'all' ? all : all.slice(-Number(range));
}
// Ranges worth a button: «30 дней» says nothing new while the whole history is shorter.
function ranges(all) {
    const out = [['7', '7 дней']];
    if (all.length > 30)
        out.push(['30', '30 дней']);
    out.push(['all', 'Всё время']);
    return out;
}
function weeks(days) {
    const out = [];
    for (const d of days) {
        const monday = new Date(parse(d.day).getTime() - weekday(d.day) * 86400e3);
        const key = `${monday.getFullYear()}-${String(monday.getMonth() + 1).padStart(2, '0')}-${String(monday.getDate()).padStart(2, '0')}`;
        let w = out[out.length - 1];
        if (!w || w.day !== key) {
            w = { day: key, usd: 0, models: {}, projects: {}, parts: [0, 0, 0, 0], tokens: 0, tokParts: [0, 0, 0, 0], tokModels: {}, tokProjects: {}, sessions: 0, started: 0 };
            out.push(w);
        }
        w.usd += d.usd;
        w.started += d.started;
        w.tokens += d.tokens;
        w.tokParts = w.tokParts.map((n, i) => n + (d.tokParts[i] ?? 0));
        for (const [m, usd] of Object.entries(d.models))
            w.models[m] = (w.models[m] ?? 0) + usd;
    }
    return out;
}
// this week vs the same weekdays of the last one: on Wednesday, Mon–Wed against Mon–Wed
function weekOverWeek(all) {
    const thisWeek = weeks(all).at(-1);
    if (!thisWeek)
        return '';
    const n = weekday(all[all.length - 1].day) + 1;
    const prev = all.filter(d => d.day < thisWeek.day).slice(-7).filter(d => weekday(d.day) < n).reduce((a, d) => a + d.usd, 0);
    if (!prev)
        return '';
    const delta = Math.round(((thisWeek.usd - prev) / prev) * 100);
    return `${delta > 0 ? '+' : ''}${delta}% к тем же дням прошлой`;
}
function vsPrev(ws, i) {
    const r = ws[i], before = ws[i - 1];
    return before && before.usd ? `<br>к прошлой: ${r.usd >= before.usd ? '+' : ''}${Math.round(((r.usd - before.usd) / before.usd) * 100)}%` : '';
}
function niceMax(v) {
    if (v <= 0)
        return 1;
    const p = 10 ** Math.floor(Math.log10(v));
    return ([1, 2, 2.5, 5, 10].find(k => k * p >= v) ?? 10) * p;
}
function tipOf(title, d, extra = '') {
    const lines = MODELS.map(f => [f, [...Object.entries(d.models)].filter(([m]) => family(m) === f)])
        .flatMap(([, ms]) => ms.map(([m, usd]) => `${modelName(m)}: ${money(usd)}`));
    return `<b>${esc(title)}</b><br>итого ${money(d.usd)}${lines.length ? '<br>' + lines.join('<br>') : ''}${extra}`;
}
const usdByModel = r => MODELS.map(f => [color(f), Object.entries(r.models).filter(([m]) => family(m) === f).reduce((a, [, v]) => a + v, 0)]);
// stacked bars, one per day or week, models in fixed order bottom-up
function barChart(rows, label, tip, h = 104, stack = usdByModel, fmt = money) {
    const top = 6, bottom = 14;
    const plot = h - top - bottom;
    const max = niceMax(Math.max(...rows.map(r => stack(r).reduce((a, [, v]) => a + v, 0)), 0));
    const left = Math.max(40, Math.ceil(Math.max(...[0, 0.5, 1].map(k => fmt(max * k).length)) * 5.5) + 6);
    const step = (W - left) / Math.max(rows.length, 1);
    const bw = Math.max(1, Math.floor(step) - (step > 6 ? 2 : 1));
    let s = '';
    for (const k of [0, 0.5, 1]) {
        const y = Math.round(top + plot - plot * k);
        s += `<rect x="${left}" y="${y}" width="${W - left}" height="1" fill="var(--line)"/>`;
        s += `<text x="${left - 4}" y="${y + 3}" text-anchor="end">${fmt(max * k)}</text>`;
    }
    rows.forEach((r, i) => {
        const x = Math.round(left + i * step + (step - bw) / 2);
        let y = top + plot;
        for (const [fill, v] of stack(r)) {
            const hh = Math.round((v / max) * plot);
            if (hh <= 0)
                continue;
            y -= hh;
            s += `<rect x="${x}" y="${y}" width="${bw}" height="${Math.max(hh - 1, 1)}" fill="${fill}"/>`;
        }
        s += `<rect class="hit" x="${Math.round(left + i * step)}" y="0" width="${Math.ceil(step)}" height="${top + plot}" data-tip="${esc(tip(r, i))}"/>`;
    });
    const marks = rows.length <= 8 ? rows.map((_, i) => i) : [0, Math.floor((rows.length - 1) / 2), rows.length - 1];
    for (const i of marks) {
        const r = rows[i];
        if (!r)
            continue;
        const x = left + i * step + step / 2;
        const anchor = i === 0 && rows.length > 8 ? 'start' : i === rows.length - 1 && rows.length > 8 ? 'end' : 'middle';
        s += `<text x="${anchor === 'start' ? left : anchor === 'end' ? W : x}" y="${h - 3}" text-anchor="${anchor}">${esc(label(r))}</text>`;
    }
    return `<svg viewBox="0 0 ${W} ${h}">${s}</svg>`;
}
function legend(fams) {
    return '<div class="legend">' + MODELS.filter(f => fams.has(f))
        .map(f => `<span><i style="background:${color(f)}"></i>${[...fams.get(f).names].join(', ')}</span>`).join('') + '</div>';
}
function heatmap(all) {
    const span = range === '7' ? 7 : 28;
    const from = all.slice(-span)[0]?.day ?? '';
    const grid = WEEKDAYS.map(() => Array(24).fill(0));
    for (const [day, hour, usd] of stats?.hours ?? []) {
        if (day >= from)
            grid[weekday(day)][hour] += usd;
    }
    const max = Math.max(...grid.flat(), 0.01);
    const left = 20, cw = Math.floor((W - left) / 24), ch = 11;
    let s = '';
    grid.forEach((row, y) => {
        s += `<text x="0" y="${y * (ch + 2) + 9}">${WEEKDAYS[y]}</text>`;
        row.forEach((usd, x) => {
            const level = usd <= 0 ? 0 : Math.ceil((usd / max) * 5);
            const fill = level ? `var(--gold)" fill-opacity="${[0, 0.18, 0.34, 0.52, 0.74, 1][level]}` : 'var(--line)';
            s += `<rect x="${left + x * cw}" y="${y * (ch + 2)}" width="${cw - 2}" height="${ch}" fill="${fill}"/>`;
            s += `<rect class="hit" x="${left + x * cw}" y="${y * (ch + 2)}" width="${cw}" height="${ch + 2}" data-tip="${esc(`<b>${WEEKDAYS[y]}, ${x}:00–${x + 1}:00</b><br>${money(usd)} за ${span} дн.`)}"/>`;
        });
    });
    const hy = 7 * (ch + 2) + 9;
    for (const hr of [0, 6, 12, 18, 23])
        s += `<text x="${left + hr * cw + (cw - 2) / 2}" y="${hy}" text-anchor="middle">${hr}</text>`;
    return `<svg viewBox="0 0 ${W} ${hy + 2}">${s}</svg>`;
}
function barList(rows, total, fmt = money) {
    const max = Math.max(...rows.map(r => r.usd), 0.01);
    return '<div class="bars">' + rows.map(r => `<div class="line"${r.tip ? ` data-tip="${esc(r.tip)}"` : ''}>
    <span class="name">${/^var\(--[mt]-/.test(r.fill) ? `<i style="background:${r.fill}"></i>` : ''}${esc(r.name)}</span>
    <span class="track"><span class="fill" style="display:block;width:${(r.usd / max) * 100}%;background:${r.fill}"></span></span>
    <span class="val">${fmt(r.usd)} <small>${pct(r.usd, total)}</small></span></div>`).join('') + '</div>';
}
const span = (days) => `${short(days[0].day)} — ${short(days[days.length - 1].day)}`;
function renderStats() {
    if (!stats)
        return filters([]) + `<div class="empty">${online ? 'Считаю траты по журналам…' : 'Сервер не отвечает'}</div>`;
    const all = stats.days;
    if (!all.length)
        return filters(all) + '<div class="empty">Журналов с тратами пока нет</div>';
    if (!ranges(all).some(([k]) => k === range))
        range = 'all';
    const days = rangeDays(all);
    const total = days.reduce((a, d) => a + d.usd, 0);
    const started = days.reduce((a, d) => a + d.started, 0);
    const active = days.filter(d => d.usd > 0).length;
    const tokensSum = days.reduce((a, d) => a + d.tokens, 0);
    let html = filters(all);
    html += `<div class="tiles four">
    <div class="tile pix"><div class="k">Потрачено</div><div class="v">${money(total)}</div><div class="s">${tokens(tokensSum)} ток.</div></div>
    <div class="tile pix"><div class="k">В день</div><div class="v">${money(active ? total / active : 0)}</div><div class="s">${active} из ${days.length} дн.</div></div>
    <div class="tile pix"><div class="k">Сессий</div><div class="v">${started}</div><div class="s">начато</div></div>
    <div class="tile pix"><div class="k">Сессия</div><div class="v">${money(started ? total / started : 0)}</div><div class="s">в среднем</div></div>
  </div>`;
    const fams = byFamily(days);
    const byWeek = days.length > 45;
    const rows = byWeek ? weeks(days) : days;
    // week over week only makes sense over the whole history, not inside a 7- or 30-day window
    const wow = range === 'all' ? weekOverWeek(all) : '';
    html += `<section><h3>${byWeek ? 'ПО НЕДЕЛЯМ' : 'ПО ДНЯМ'} <span class="hint">${byWeek && wow ? wow : span(days)}</span></h3><div class="chart pix">`;
    html += barChart(rows, r => short(r.day), (r, i) => tipOf(byWeek ? 'неделя с ' + short(r.day) : `${WEEKDAYS[weekday(r.day)]}, ${short(r.day)}`, r, (byWeek ? vsPrev(rows, i) : '') + `<br>сессий: ${r.started}`));
    html += legend(fams) + '</div></section>';
    // the main chart is daily here, so the weeks get their own; by week it already shows them
    const ws = weeks(all);
    if (range === 'all' && !byWeek && ws.length > 1) {
        html += `<section><h3>НЕДЕЛЯ К НЕДЕЛЕ <span class="hint">${wow}</span></h3><div class="chart pix">`;
        html += barChart(ws, r => short(r.day), (r, i) => tipOf('неделя с ' + short(r.day), r, vsPrev(ws, i) + `<br>сессий: ${r.started}`), 84);
        html += '</div></section>';
    }
    html += '<section><h3>МОДЕЛИ</h3>';
    html += barList(MODELS.filter(f => fams.has(f)).map(f => familyRow(f, fams.get(f))), total);
    html += '</section>';
    const parts = [0, 1, 2, 3].map(i => days.reduce((a, d) => a + (d.parts[i] ?? 0), 0));
    const partNames = ['Вход', 'Ответы модели', 'Чтение кэша', 'Запись в кэш'];
    const partTips = ['новый текст в контексте', 'то, что пишет модель', 'повторное чтение контекста — растёт с длиной сессии', 'кэширование контекста на 5 мин или час'];
    html += '<section><h3>ЗА ЧТО ПЛАТИМ <span class="hint">наведите на строку</span></h3>';
    html += barList(partNames.map((name, i) => ({ name, usd: parts[i] ?? 0, fill: '#7a80a8', tip: `<b>${name}</b><br>${partTips[i]}` })), total);
    html += '</section>';
    html += `<section><h3>ТРАТЫ ПО ЧАСАМ <span class="hint">дни недели × часы, за ${range === '7' ? 7 : 28} дн.</span></h3><div class="chart pix">${heatmap(all)}</div></section>`;
    html += renderLimits();
    const projects = new Map();
    for (const d of days)
        for (const [p, usd] of Object.entries(d.projects))
            projects.set(p, (projects.get(p) ?? 0) + usd);
    const top = [...projects].sort((a, b) => b[1] - a[1]);
    const shown = top.slice(0, 6).map(([name, usd]) => ({ name, usd, fill: 'var(--gold)' }));
    const other = top.slice(6).reduce((a, [, usd]) => a + usd, 0);
    if (other > 0)
        shown.push({ name: `прочие (${top.length - 6})`, usd: other, fill: 'var(--muted)' });
    html += '<section><h3>ПРОЕКТЫ <span class="hint">по папке, где запущена сессия</span></h3>' + barList(shown, total) + '</section>';
    html += historyNote();
    return html;
}
// ── Tokens tab: the same history counted in tokens, no dollars ──
const KINDS = [
    { name: 'Вход', fill: 'var(--t-in)', tip: 'новый текст в контексте: ваши сообщения, файлы, вывод команд' },
    { name: 'Ответы модели', fill: 'var(--t-out)', tip: 'то, что пишет модель: текст, код, вызовы инструментов' },
    { name: 'Чтение кэша', fill: 'var(--t-read)', tip: 'контекст, перечитанный из кэша на каждом шаге, — растёт с длиной сессии' },
    { name: 'Запись в кэш', fill: 'var(--t-write)', tip: 'контекст, положенный в кэш на 5 минут или час' },
];
// history from before the split knows only the total
const unsplit = (d) => Math.max(0, d.tokens - d.tokParts.reduce((a, n) => a + n, 0));
const tokByKind = r => [...KINDS.map((k, i) => [k.fill, r.tokParts[i] ?? 0]), ['var(--muted)', unsplit(r)]];
function tokenTip(title, d, extra = '') {
    const lines = KINDS.map((k, i) => [k.name, d.tokParts[i] ?? 0]).filter(([, n]) => n > 0)
        .map(([name, n]) => `${name}: ${tokens(n)}`);
    if (unsplit(d))
        lines.push(`без разбивки: ${tokens(unsplit(d))}`);
    return `<b>${esc(title)}</b><br>итого ${tokens(d.tokens)}${lines.length ? '<br>' + lines.join('<br>') : ''}${extra}`;
}
// «4.4 млрд» does not fit a third of the popover in the big font: the unit goes small
function big(n) {
    const [num, unit] = tokens(n).split(' ');
    return unit ? `${num}<small>${unit}</small>` : num;
}
function renderTokens() {
    if (!stats)
        return filters([]) + `<div class="empty">${online ? 'Считаю токены по журналам…' : 'Сервер не отвечает'}</div>`;
    const all = stats.days;
    if (!all.length)
        return filters(all) + '<div class="empty">Журналов с токенами пока нет</div>';
    if (!ranges(all).some(([k]) => k === range))
        range = 'all';
    const days = rangeDays(all);
    const sum = (ds) => ds.reduce((a, d) => a + d.tokens, 0);
    const total = sum(days);
    const active = days.filter(d => d.tokens > 0).length;
    const started = days.reduce((a, d) => a + d.started, 0);
    const out = days.reduce((a, d) => a + (d.tokParts[1] ?? 0), 0);
    let html = filters(all);
    html += `<div class="tiles">
    <div class="tile pix"><div class="k">Всего</div><div class="v">${big(total)}</div><div class="s">за ${days.length} дн.</div></div>
    <div class="tile pix"><div class="k">В день</div><div class="v">${big(active ? total / active : 0)}</div><div class="s">${active} из ${days.length} дн.</div></div>
    <div class="tile pix"><div class="k">Сессия</div><div class="v">${big(started ? total / started : 0)}</div><div class="s">в среднем из ${started}</div></div>
  </div>`;
    const byWeek = days.length > 45;
    const rows = byWeek ? weeks(days) : days;
    html += `<section><h3>${byWeek ? 'ПО НЕДЕЛЯМ' : 'ПО ДНЯМ'} <span class="hint">${span(days)}</span></h3><div class="chart pix">`;
    html += barChart(rows, r => short(r.day), r => tokenTip(byWeek ? 'неделя с ' + short(r.day) : `${WEEKDAYS[weekday(r.day)]}, ${short(r.day)}`, r), 104, tokByKind, tokens);
    html += '<div class="legend">' + KINDS.map(k => `<span><i style="background:${k.fill}"></i>${k.name}</span>`).join('') + '</div></div></section>';
    const parts = KINDS.map((k, i) => ({ name: k.name, usd: days.reduce((a, d) => a + (d.tokParts[i] ?? 0), 0), fill: k.fill, tip: `<b>${k.name}</b><br>${k.tip}` }));
    const rest = days.reduce((a, d) => a + unsplit(d), 0);
    if (rest > 0)
        parts.push({ name: 'Без разбивки', usd: rest, fill: 'var(--muted)', tip: '<b>Без разбивки</b><br>история, журналы которой Claude Code уже удалил: известно только общее число' });
    html += '<section><h3>ТИПЫ ТОКЕНОВ <span class="hint">наведите на строку</span></h3>';
    html += barList(parts, total, tokens) + '</section>';
    html += `<p class="note">Модель написала <b>${tokens(out)}</b> — ${pct(out, total)} от всех токенов периода. Остальное — контекст, который она читает на каждом шаге.</p>`;
    const fams = byFamily(days, d => d.tokModels);
    html += '<section><h3>МОДЕЛИ</h3>';
    html += barList(MODELS.filter(f => fams.has(f)).map(f => familyRow(f, fams.get(f))), total, tokens);
    html += '</section>';
    const projects = new Map();
    for (const d of days)
        for (const [p, n] of Object.entries(d.tokProjects))
            projects.set(p, (projects.get(p) ?? 0) + n);
    const top = [...projects].sort((a, b) => b[1] - a[1]);
    const shown = top.slice(0, 6).map(([name, n]) => ({ name, usd: n, fill: 'var(--gold)' }));
    const other = top.slice(6).reduce((a, [, n]) => a + n, 0);
    if (other > 0)
        shown.push({ name: `прочие (${top.length - 6})`, usd: other, fill: 'var(--muted)' });
    html += '<section><h3>ПРОЕКТЫ <span class="hint">по папке, где запущена сессия</span></h3>' + barList(shown, total, tokens) + '</section>';
    return html;
}
// ── Render ──
function render() {
    renderState();
    document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    const main = $('main');
    const y = window.scrollY;
    main.innerHTML = tab === 'stats' ? renderStats() : tab === 'tokens' ? renderTokens() : renderSessions();
    window.scrollTo(0, y);
    $('autostart').classList.toggle('on', autostart);
    fit();
}
// ── Events ──
document.addEventListener('click', e => {
    const t = e.target;
    const open = t.closest('[data-open]');
    if (open)
        return openPath(open.dataset.open);
    const tabBtn = t.closest('[data-tab]');
    if (tabBtn) {
        tab = tabBtn.dataset.tab;
        keep('tab', tab);
        window.scrollTo(0, 0);
        render();
        if (tab !== 'sessions')
            refresh(true);
        return;
    }
    const r = t.closest('[data-range]');
    if (r) {
        range = r.dataset.range;
        keep('range', range);
        return render();
    }
    const p = t.closest('[data-provider]');
    if (p) {
        provider = provider === p.dataset.provider ? 'all' : p.dataset.provider;
        keep('provider', provider);
        refresh(true);
    }
});
document.addEventListener('toggle', e => {
    const d = e.target;
    if (d.id === 'rest') {
        keep('rest', d.open ? '1' : '');
        fit();
    }
}, true);
const tip = $('tip');
document.addEventListener('mousemove', e => {
    const t = e.target.closest?.('[data-tip]');
    const html = t?.getAttribute('data-tip');
    if (!html) {
        tip.style.display = 'none';
        return;
    }
    tip.innerHTML = html;
    tip.style.display = 'block';
    const r = tip.getBoundingClientRect();
    const x = Math.min(e.clientX + 12, window.innerWidth - r.width - 6);
    const yy = e.clientY + 14 + r.height > window.innerHeight ? e.clientY - r.height - 10 : e.clientY + 14;
    tip.style.left = Math.max(6, x) + 'px';
    tip.style.top = yy + 'px';
});
document.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
$('office').onclick = () => openPath('/');
$('log').onclick = () => send('log');
$('quit').onclick = () => send('quit');
$('autostart').onclick = () => send('autostart');
// the app calls these when the popover opens and closes
const w = window;
w.aoShow = (state) => {
    autostart = !!state?.autostart;
    active = true;
    refresh(true);
};
w.aoHide = () => { active = false; tip.style.display = 'none'; };
if (bridge)
    document.body.classList.add('app');
send('ready');
$('logo').innerHTML = logo();
render();
refresh(true);
setInterval(() => { if (active)
    refresh(); }, 5000);
