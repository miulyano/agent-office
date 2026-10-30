// The office as a building: one floor per department, a desk cell per agent,
// the producer's office on top, the lobby with the lounge at the bottom.
// Drawn on a canvas every frame; text goes to an overlay canvas so it stays sharp.
// Agents move only inside their own cell, along their floor's corridor and in the lift.

import type { Agent, Head, OfficeDesk, Shell, Studio } from './types'
import { GUESTS, deskName, deskOf, money, span, workedNow } from './office.js'

// ── data → people ──────────────────────────────────────────────────────────
type Grade = 'intern' | 'middle' | 'senior' | 'lead'
type Look = { id: string; skin: string; hair: string; shirt: string; pants: string; cap: string; crown: boolean }
// waiting: a desk whose agent this session has not called yet — it stands empty with its name plate
type Person = { key: string; name: string; grade: Grade; model: string; look: Look; boss: boolean; agent?: Agent; head?: Head; desk?: boolean; waiting?: boolean }
type Cell = { key: string; person?: Person; name: string }
type FloorDef = { name: string; cells: Cell[]; free?: boolean; home: number; note?: string; strip?: boolean; track?: Array<[string, string]> | null }

const SKIN = ['#f1c27d', '#e0ac69', '#c68642', '#ffdbac', '#8d5524']
const HAIRS = ['#2b1d14', '#1a1a1a', '#6b4a2b', '#c9a227', '#9a9a9a', '#8a5a2b', '#3a2416', '#e05a8a']
const CAPS = ['#d06a2a', '#2f7a5a', '#c03040', '#3a6ab0']
const INK = '#1a1a22', GOLD = '#f0c040'
const MARKS = '①②③④⑤⑥⑦⑧⑨'
// in a task of several sessions, people from a started session wear its number; the root's own go unmarked, as in the server's log
const markOf = (st: Studio, n: number) => n > 1 && (st.sessions?.length ?? 1) > 1 ? MARKS[Math.min(n, MARKS.length) - 1] + ' ' : ''

// the model is the grade: Haiku in a cap, Sonnet in a hoodie, Opus in a suit, Fable in a dark suit with glasses;
// Codex models take the same grades by their tier
const MODEL_GRADE: Array<[string, Grade, string]> = [['haiku', 'intern', 'Haiku'], ['sonnet', 'middle', 'Sonnet'], ['opus', 'senior', 'Opus'], ['fable', 'lead', 'Fable'],
  ['luna', 'intern', 'Luna'], ['terra', 'middle', 'Terra'], ['sol', 'senior', 'Sol'], ['astra', 'lead', 'Astra'], ['auto-review', 'intern', 'Авторевью']]
const MODEL_COLOR: Record<Grade, string> = { intern: '#8af0a0', middle: '#7fdcff', senior: '#ffb070', lead: '#f0c040' }
function gradeOf(model: string): [Grade, string] {
  const m = MODEL_GRADE.find(([k]) => model.includes(k))
  return m ? [m[1], m[2]] : ['middle', model ? model.replace(/^claude-/, '').replace(/^gpt-/, 'GPT-') : '—']
}

function hash(s: string): number {
  let n = 0
  for (const c of s) n = (n * 31 + c.charCodeAt(0)) >>> 0
  return n
}

function lookOf(key: string, desk: OfficeDesk | undefined, shirt: string, crown = false): Look {
  const h = hash(key)
  return {
    id: key,
    skin: SKIN[(desk ? desk.skin : h) % SKIN.length]!,
    hair: desk?.hair ?? HAIRS[h % HAIRS.length]!,
    shirt: desk?.shirt ?? shirt,
    pants: h % 2 ? '#2a2e44' : '#3a3a50',
    cap: CAPS[h % CAPS.length]!,
    crown,
  }
}

const AWAY = 30 * 60e3, DONE = 15e3, THINK = 60e3, COFFEE = 3 * 60e3

// Floors from the folder's office: its zones, then the free-lance floor for built-in agents.
// A floor never folds: every desk keeps its cell. A desk's owner is the newest agent of its type;
// one who went home leaves an empty chair with a sign, one never called leaves the desk empty.
// Extra copies of a type working at once get extra cells; built-in agents get cells of their own.
const FREE_HOME = 4  // free-lancers gone home that still keep a cell; the rest are only counted

type Built = { boss: Person; heads: Person[]; floors: FloorDef[] }

function building(st: Studio, now: number): Built {
  const office = st.office
  const mainDesk = deskOf(office, 'main')
  const heads = st.sessions ?? []
  // the root's head is the boss; the heads of the sessions it started sit beside in the same room
  const chief = (h: Head | undefined, i: number): Person => {
    const [g, m] = gradeOf(h?.model ?? st.producer.model)
    const key = i === 0 ? 'boss' : 'head:' + h!.id
    return { key, name: markOf(st, i + 1) + deskName('main', office), grade: g, model: m, boss: true, head: h, look: lookOf(key, i === 0 ? mainDesk : undefined, '#3b3f8f', i === 0) }
  }
  const boss = chief(heads[0], 0)
  const floors: FloorDef[] = office.zones
    .map(z => ({ name: z.name, note: z.note, strip: z.strip, track: z.track, home: 0, cells: z.strip ? [] : z.desks.filter(d => d.type !== 'main').map(d => ({ key: d.type, name: d.short ?? d.name } as Cell)) }))
  // built-in agents have no desk but keep a cell on the free-lance floor, waiting like a desk's owner
  const free: FloorDef = { name: 'ФРИЛАНС', cells: Object.entries(office.guests).map(([t, g]) => ({ key: t, name: g.name } as Cell)), free: true, home: 0 }
  const gone = (a: Agent) => !a.isWorking && now - a.endedAt >= AWAY
  // at work first, then home, newest first: a desk goes to whoever is here
  const order = [...st.agents].sort((x, y) => Number(gone(x)) - Number(gone(y)) || y.startedAt - x.startedAt)
  const extra: Array<[FloorDef, Cell]> = []
  for (const a of order) {
    const desk = deskOf(office, a.type)
    const own = desk && desk.type !== 'main' ? desk : undefined
    const [grade, model] = gradeOf(a.model)
    const name = markOf(st, a.session ?? 1) + deskName(a.type, office)
    const fi = own ? office.zones.findIndex(z => z.desks.includes(own)) : -1
    const floor = fi >= 0 ? floors[fi]! : free
    const seat = floor.cells.find(c => c.key === (own ? own.type : a.type))
    // the owner of a desk or of a guest cell looks the same every time they are called
    const home = own ? own.type : seat ? a.type : undefined
    const person: Person = { key: a.id, name, grade, model, boss: false, agent: a, desk: !!home, look: lookOf(home ?? a.id, own, GUESTS[a.type]?.shirt ?? '#5a6a8a') }
    if (seat && !seat.person) { seat.person = person; if (gone(a)) floor.home++; continue }
    // a folded floor draws no desks: its people are only counted
    if (floor.strip) { floor.home++; continue }
    if (gone(a)) {
      // a copy that went home: its desk is taken or it is a free-lancer
      if (!own && free.cells.filter(c => c.person && gone(c.person.agent!)).length < FREE_HOME) free.cells.push({ key: a.id, person, name })
      floor.home++
      continue
    }
    extra.push([floor, { key: a.id, person, name }])
  }
  // free-lancers at work sit before those gone home, in the order they came
  for (const [floor, cell] of extra.reverse()) {
    if (floor === free) free.cells.unshift(cell)
    else floor.cells.push(cell)
  }
  for (const fl of floors) for (const cell of fl.cells) {
    if (cell.person) continue
    const desk = deskOf(office, cell.key)
    cell.person = { key: 'wait:' + cell.key, name: cell.name, grade: 'middle', model: '', boss: false, desk: true, waiting: true, look: lookOf(cell.key, desk, GUESTS[cell.key]?.shirt ?? '#5a6a8a') }
  }
  if (free.cells.length || free.home) floors.push(free)
  return { boss, heads: heads.slice(1).map((h, i) => chief(h, i + 1)), floors: floors.filter(f => f.cells.length || f.home || f.strip) }
}

// ── what a person is doing right now ───────────────────────────────────────
type Place = 'desk' | 'cooler' | 'couch' | 'gone'
type Pose = 'sit' | 'type' | 'think' | 'drink' | 'phone' | 'sleep' | 'walk' | 'standDrink'
type Doing = { place: Place; pose: Pose; icon: string; text: string; work: boolean; time?: string }
const TIME_COLOR = '#bfe3ff'   // the clock and its figure: apart from the status text and the model's colour

const clockOf = (at: number) => new Date(at).toTimeString().slice(0, 5)

const ICON: Record<string, string> = { read: 'read', edit: 'edit', write: 'write', grep: 'grep', bash: 'bash', web: 'web', agent: 'phone', skill: 'skill', other: 'tool' }

function busy(tool: string, text: string): Doing {
  const icon = ICON[tool] ?? 'tool'
  return { place: 'desk', pose: icon === 'phone' ? 'phone' : 'type', icon, text, work: true }
}

function doing(p: Person, st: Studio, now: number): Doing {
  if (p.boss) {
    const pr = p.head ?? st.producer
    // the clock of the current request: it runs while the head is busy
    const since = p.head?.since ?? st.since
    const timed = (d: Doing): Doing => ({ ...d, time: since ? span(now - since) : undefined })
    if (pr.isBusy && now - pr.at < THINK) return timed(busy(pr.tool, pr.activity))
    if (pr.isBusy) return timed({ place: 'desk', pose: 'think', icon: 'think', text: 'думает', work: true })
    // a coordinator of an epic is not idle while the owner owes an answer, or while its roles work in their own sessions
    if (st.producer.owner) return { place: 'desk', pose: 'phone', icon: 'phone', text: 'нужен владелец', work: false }
    const session = Math.max(1, (st.sessions ?? []).findIndex(h => h === p.head) + 1)
    const waits = (st.shells ?? []).filter(s => s.status === 'running' && s.session === session)
    if (waits.length) return { place: 'desk', pose: 'think', icon: 'think', text: 'ждёт фон', work: false, time: span(now - waits[0]!.start) }
    if (st.producer.waitingFor) return { place: 'desk', pose: 'phone', icon: 'phone', text: 'ждёт: ' + st.producer.waitingFor, work: false }
    return { place: 'desk', pose: 'drink', icon: 'coffee', text: 'ждёт задачу', work: false }
  }
  if (p.waiting) return { place: 'desk', pose: 'sit', icon: '', text: 'ждёт вызова', work: false }
  const a = p.agent!
  // an agent's clock: how long it has worked; once done, how long it took; gone home, since when
  const home: Doing = { place: 'gone', pose: 'sit', icon: 'home', text: 'дома с', work: false, time: clockOf(a.endedAt || a.at || a.startedAt) }
  const took = span((a.endedAt || a.at) - a.startedAt), sofar = span(now - a.startedAt)
  // an epic role's job file outranks the silence in its journal: a long test is still work
  if (a.job === 'working') return { ...busy(a.tool, a.activity || a.desc), time: sofar }
  if (a.job === 'idle') return { place: 'desk', pose: 'sit', icon: 'think', text: 'ждёт', work: false, time: sofar }
  if (a.finished) {
    const since = now - a.endedAt
    if (since < DONE) return { place: 'desk', pose: 'sit', icon: 'done', text: 'сдал работу', work: false, time: took }
    if (since < AWAY) return { place: 'couch', pose: 'sit', icon: 'rest', text: 'в лаунже', work: false, time: took }
    return home
  }
  const idle = now - (a.at || a.startedAt)
  if (a.isWorking && idle < THINK) return { ...busy(a.tool, a.activity || a.desc), time: sofar }
  if (a.isWorking && idle < COFFEE) return { place: 'desk', pose: 'think', icon: 'think', text: 'думает', work: true, time: sofar }
  if (a.isWorking) return { place: 'cooler', pose: 'standDrink', icon: 'coffee', text: 'у кулера', work: false, time: sofar }
  if (now - a.endedAt < AWAY) return { place: 'desk', pose: 'sleep', icon: 'sleep', text: 'спит', work: false, time: took }
  return home
}

// ── trips: corridor → lift → lobby, and back ───────────────────────────────
const WALK = 7
const TRIP = {
  down: { floor: [0, 0.38], board: [0.38, 0.44], ride: [0.44, 0.78], lobby: [0.78, 1] },
  up: { lobby: [0, 0.22], board: [0.22, 0.28], ride: [0.28, 0.62], floor: [0.62, 1] },
} as const
type Phase = { name: 'floor' | 'board' | 'ride' | 'lobby'; k: number }
function tripPhase(down: boolean, q: number): Phase {
  const P = down ? TRIP.down : TRIP.up
  for (const [name, [a, b]] of Object.entries(P) as Array<[Phase['name'], readonly [number, number]]>) if (q >= a && q < b) return { name, k: (q - a) / (b - a) }
  return { name: down ? 'lobby' : 'floor', k: 1 }
}
type Trip = { from: Place; to: Place; t0: number; fromSeat: number; toSeat: number; lane: number }
const rides = (tr: Trip) => tr.from === 'desk' || tr.to === 'desk'   // inside the lobby people walk
function tripText(tr: Trip, riding: boolean): string {
  if (tr.to === 'desk') return 'идёт к столу'
  if (tr.to === 'gone') return 'уходит домой'
  if (!rides(tr)) return tr.to === 'couch' ? 'идёт к дивану' : 'идёт к кулеру'
  return riding ? 'едет в лаунж' : 'идёт в лаунж'
}
type Memory = { place: Place; seat: number; trip?: Trip; finished: boolean }

// ── styles: office type and time of day ────────────────────────────────────
type Fig = { key: string; head: [number, number]; torso: [number, number]; thigh: number; shin: number; outline: string | null; eyes: string; blush?: boolean; hairRows: number; shade?: number; rim?: boolean; bubble: 'round' | 'box' | 'holo' }
const FIGS: Record<string, Fig> = {
  chibi: { key: 'chibi', head: [10, 9], torso: [7, 6], thigh: 4, shin: 4, outline: '#1a1410', eyes: 'big', blush: true, hairRows: 3, bubble: 'round' },
  rpg: { key: 'rpg', head: [8, 8], torso: [7, 8], thigh: 5, shin: 5, outline: 'dark', eyes: 'tall', hairRows: 3, shade: 1, bubble: 'box' },
  hibit: { key: 'hibit', head: [6, 7], torso: [6, 11], thigh: 6, shin: 7, outline: null, eyes: 'dot', hairRows: 2, shade: 2, rim: true, bubble: 'holo' },
}
type Room = { name: string; ref: string; fig: string; wall: string; wall2: string; floor: string; desk: string; deskD: string; deskTop: string; chair: string; pc: string; pcD: string; off: string; line: string | null; door: string; cooler: string; couch: string; facade: string; slab: string; lampsAlways?: boolean }
export const OFFICES: Record<string, Room> = {
  cozy: { name: 'Уютный', ref: 'дерево и тёплый свет, в духе игр Kairosoft', fig: 'chibi', wall: '#f2ddb2', wall2: '#e3c992', floor: '#cf9a5e', desk: '#b8702f', deskD: '#7a4418', deskTop: '#d89048', chair: '#5a4a3a', pc: '#e8e0cc', pcD: '#a8a08c', off: '#3a4a4a', line: '#2a1a10', door: '#8a5428', cooler: '#e8e8e8', couch: '#c0505a', facade: '#7a4a3a', slab: '#5a3a2c' },
  business: { name: 'Бизнес-центр', ref: 'светлые стены и ковролин, в духе Pokémon GBA', fig: 'rpg', wall: '#dcecd8', wall2: '#c4dcc0', floor: '#8aa0c8', desk: '#c8c8d4', deskD: '#8888a0', deskTop: '#e2e2ec', chair: '#4a5a7a', pc: '#d8d8e0', pcD: '#9898a8', off: '#304050', line: '#404848', door: '#6a7a9a', cooler: '#f0f0f0', couch: '#5a7ac0', facade: '#4a5a6a', slab: '#5a6878' },
  loft: { name: 'Лофт', ref: 'кирпич и лампы, в духе Eastward', fig: 'hibit', wall: '#26414a', wall2: '#1f3840', floor: '#4e3626', desk: '#7a5034', deskD: '#4a2e1c', deskTop: '#8e6040', chair: '#2e3a40', pc: '#2a2a30', pcD: '#18181c', off: '#10181c', line: null, door: '#3a2a20', cooler: '#9aa8b0', couch: '#6a3a3a', facade: '#1c262a', slab: '#2a2420', lampsAlways: true },
}
type Time = { name: string; sky: [string, string]; tint: string | null; lamps: boolean; sun?: [number, number]; moon?: boolean }
const TIMES: Record<string, Time> = {
  morning: { name: 'Утро', sky: ['#bfe0ff', '#ffd2a0'], tint: '#fff0dc', lamps: false, sun: [0.15, 0.7] },
  day: { name: 'День', sky: ['#6cbcff', '#bfe6ff'], tint: null, lamps: false, sun: [0.5, 0.25] },
  evening: { name: 'Вечер', sky: ['#4a2f7a', '#ff9a6a'], tint: '#dcb8c8', lamps: true, sun: [0.85, 0.75] },
  night: { name: 'Ночь', sky: ['#060a20', '#141c44'], tint: '#7880b4', lamps: true, moon: true },
}
export const TIME_OPTIONS: Record<string, string> = { clock: 'По часам', morning: 'Утро', day: 'День', evening: 'Вечер', night: 'Ночь' }
function timeNow(sel: string): string {
  if (sel !== 'clock') return sel
  const h = new Date().getHours()
  return h >= 6 && h < 11 ? 'morning' : h >= 11 && h < 18 ? 'day' : h >= 18 && h < 22 ? 'evening' : 'night'
}

// ── pixels ─────────────────────────────────────────────────────────────────
type C2 = CanvasRenderingContext2D
const cl = (v: number) => Math.max(0, Math.min(255, Math.round(v)))
function rgb(h: string): [number, number, number] { const n = parseInt(h.slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255] }
function hex(r: number, g: number, b: number): string { return '#' + ((1 << 24) | (cl(r) << 16) | (cl(g) << 8) | cl(b)).toString(16).slice(1) }
function shade(h: string, k: number): string { if (h[0] !== '#') return h; const [r, g, b] = rgb(h); return hex(r * k, g * k, b * k) }
function R(c: C2, x: number, y: number, w: number, h: number, col: string) { if (w <= 0 || h <= 0) return; c.fillStyle = col; c.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h)) }
function box(c: C2, x: number, y: number, w: number, h: number, fill: string, line: string | null) { if (line) { R(c, x, y, w, h, line); R(c, x + 1, y + 1, w - 2, h - 2, fill) } else R(c, x, y, w, h, fill) }

// ── figures: drawn into a buffer, then the style's technique ───────────────
const SB = 48, HX = 22, HY = 34
const deskH = (f: Fig) => Math.max(3, Math.min(9, Math.round(f.torso[1] * 0.55) + 1))
const sprites = new Map<string, HTMLCanvasElement>()
function sprite(f: Fig, pose: Pose, frame: number, grade: Grade, look: Look, lit: boolean): HTMLCanvasElement {
  const key = [f.key, pose, frame, grade, look.id, lit ? 1 : 0].join('|')
  let c = sprites.get(key)
  if (c) return c
  c = document.createElement('canvas'); c.width = SB; c.height = SB
  const g = c.getContext('2d')!
  drawFigure(g, f, pose, frame, grade, look)
  post(g, f, lit)
  if (sprites.size > 4000) sprites.clear()
  sprites.set(key, c)
  return c
}

function drawFigure(g: C2, f: Fig, pose: Pose, frame: number, grade: Grade, look: Look) {
  const [hw, hh] = f.head, [tw, th] = f.torso
  const lt = Math.max(1, Math.round(tw * 0.45)), at = Math.max(1, Math.round(tw * 0.3))
  const suited = grade === 'senior' || grade === 'lead'
  const suit = grade === 'senior' ? '#3a4058' : grade === 'lead' ? '#23232f' : look.shirt
  const standing = pose === 'walk' || pose === 'standDrink'
  const hx = HX, hy = standing ? SB - 1 - f.thigh - f.shin : HY
  const lean = pose === 'think' ? -1 : pose === 'sleep' ? Math.max(2, Math.round(tw * 0.5)) : 0
  if (standing) {
    const len = f.thigh + f.shin, sw = pose === 'walk' ? ([[-1, 1], [0, 0], [1, -1], [0, 0]] as const)[frame % 4]! : [0, 0] as const
    const bl = hx - Math.floor(tw / 2), fl = bl + tw - lt
    R(g, bl + sw[0], hy, lt, len, shade(look.pants, 0.75)); R(g, bl + sw[0], hy + len - 1, lt + 1, 1, INK)
    R(g, fl + sw[1], hy, lt, len, look.pants); R(g, fl + sw[1], hy + len - 1, lt + 1, 1, INK)
  } else {
    R(g, hx - 1, hy - lt, f.thigh + lt + 1, lt, look.pants)
    R(g, hx + f.thigh, hy - lt, lt, f.shin + lt, look.pants)
    R(g, hx + f.thigh, hy + f.shin - 1, lt + 1, 1, INK)
  }
  const tx = hx - Math.floor(tw / 2)
  for (let i = 0; i < th; i++) {
    const y = hy - 1 - i, x = tx + Math.round(lean * i / th)
    R(g, x, y, tw, 1, suit)
    if (suited && tw >= 4) {
      if (i >= th - 2) R(g, x + tw - 2, y, 2, 1, '#f0f0f0')
      else if (i >= Math.floor(th * 0.3)) R(g, x + tw - 2, y, 1, 1, grade === 'lead' ? GOLD : '#c03040')
    }
  }
  const top = hy - th, topX = tx + lean
  let hX = topX + Math.round((tw - hw) / 2) + (hw > tw ? 1 : 0), hY = top - hh
  if (pose === 'sleep') { hY = top - Math.round(hh * 0.45); hX = topX + tw - Math.round(hw * 0.45) }
  const sx = topX + tw - at, sy = top + 1
  const hand = (x: number, y: number) => R(g, x, y, at, at, look.skin)
  if (pose === 'sleep') R(g, hX - 2, hY + hh - at, hw + 3, at, suit)
  R(g, hX, hY, hw, hh, look.skin)
  const hr = f.hairRows
  R(g, hX, hY, hw, hr, look.hair)
  R(g, hX, hY, Math.max(1, Math.round(hw * 0.35)), Math.max(hr, Math.round(hh * 0.6)), look.hair)
  if (f.key === 'chibi') { R(g, hX - 1, hY + 1, 1, Math.round(hh * 0.6), look.hair); R(g, hX + 1, hY - 1, hw - 2, 1, look.hair) }
  const ey = hY + Math.floor(hh / 2), ex = hX + hw - 2
  if (pose === 'sleep') R(g, ex - 1, ey, 2, 1, INK)
  else if (f.eyes === 'big') { R(g, ex - 1, ey - 1, 2, 2, INK); R(g, ex - 1, ey - 1, 1, 1, '#ffffff') }
  else if (f.eyes === 'tall') R(g, ex, ey - 1, 1, 2, INK)
  else R(g, ex, ey, 1, 1, INK)
  if (f.blush && pose !== 'sleep') R(g, ex - 2, ey + 2, 2, 1, '#f08a8a')
  if (grade === 'lead' && pose !== 'sleep') R(g, ex - 2, ey, 3, 1, INK)
  if (grade === 'intern') { const ch = Math.max(1, hr); R(g, hX, hY - 1, hw, ch, look.cap); R(g, hX + hw - 1, hY + ch - 1, Math.max(2, Math.round(hw * 0.35)), 1, look.cap) }
  if (grade === 'middle') R(g, hX - 2, hY + Math.round(hh * 0.45), 2, hh - Math.round(hh * 0.45) + 1, suit)
  if (look.crown) { R(g, hX, hY - 2, hw, 1, GOLD); for (let i = 0; i < hw; i += 2) R(g, hX + i, hY - 3, 1, 1, GOLD) }
  const dh = deskH(f)
  switch (pose) {
    case 'type': {
      const ky = hy - dh - at - (frame % 2), kx = hx + f.thigh + 2
      R(g, sx, Math.min(sy, ky), at, Math.abs(ky - sy) + 1, suit); R(g, sx, ky, kx - sx, at, suit); hand(kx, ky); break
    }
    case 'think': {
      const cx = Math.max(sx + 1, hX + hw - 2), cy = hY + hh
      R(g, sx, sy + 1, cx - sx + 1, at, suit); R(g, cx, cy, at, Math.max(1, sy + 2 - cy), suit); hand(cx, cy - at); break
    }
    case 'drink': case 'standDrink': {
      const cx = sx + at + 1, cy = hY + Math.floor(hh / 2)
      R(g, sx, sy + 1, at + 1, at, suit); R(g, cx, cy + 2, at, Math.max(1, sy - cy), suit)
      R(g, cx + at, cy - 1, 3, 3, '#f4f4f4'); R(g, cx + at, cy - 1, 3, 1, '#7a4a2a'); break
    }
    case 'phone': {
      const py = hY + 2
      R(g, sx, py + 2, at, Math.max(1, sy - py - 1), suit); hand(sx, py); R(g, sx + at, py - 1, 1, Math.max(2, hh - 3), INK); break
    }
    case 'sleep': break
    case 'walk': { const s = [1, 0, -1, 0][frame % 4]!; R(g, sx - 1 + s, sy, at, Math.round(th * 0.75), suit); hand(sx - 1 + s, sy + Math.round(th * 0.75)); break }
    default: R(g, sx - 1, sy, at, Math.round(th * 0.7), suit); hand(sx - 1, sy + Math.round(th * 0.7))
  }
}

function post(g: C2, f: Fig, lit: boolean) {
  const img = g.getImageData(0, 0, SB, SB), d = img.data, src = new Uint8ClampedArray(d)
  const at = (x: number, y: number) => (y * SB + x) * 4
  const solid = (x: number, y: number) => x >= 0 && y >= 0 && x < SB && y < SB && src[at(x, y) + 3]! > 0
  for (let y = 0; y < SB; y++) for (let x = 0; x < SB; x++) {
    const i = at(x, y)
    if (src[i + 3]) {
      const front = !solid(x + 1, y), back = !solid(x - 1, y)
      let r = src[i]!, gg = src[i + 1]!, b = src[i + 2]!
      if (f.shade === 1 && back) { r *= 0.72; gg *= 0.72; b *= 0.78 }
      if (f.shade === 2) { if (front) { r *= 1.22; gg *= 1.22; b *= 1.18 } else if (back) { r *= 0.62; gg *= 0.62; b *= 0.72 } }
      if (f.rim && lit && front) { r = r * 0.4 + 66; gg = gg * 0.4 + 139; b = b * 0.4 + 153 }
      d[i] = cl(r); d[i + 1] = cl(gg); d[i + 2] = cl(b)
    } else if (f.outline) {
      const n = ([[1, 0], [-1, 0], [0, 1], [0, -1]] as const).find(([dx, dy]) => solid(x + dx, y + dy))
      if (!n) continue
      let col: number[]
      if (f.outline === 'dark') { const j = at(x + n[0], y + n[1]); col = [src[j]! * 0.4, src[j + 1]! * 0.4, src[j + 2]! * 0.5] }
      else col = rgb(f.outline)
      d[i] = cl(col[0]!); d[i + 1] = cl(col[1]!); d[i + 2] = cl(col[2]!); d[i + 3] = 255
    }
  }
  g.putImageData(img, 0, 0)
}

// x is the hip; y is the hip when seated or the floor when standing
function blit(c: C2, spr: HTMLCanvasElement, x: number, y: number, standing: boolean, flip: boolean) {
  const dy = Math.round(standing ? y - SB + 1 : y - HY)
  if (!flip) { c.drawImage(spr, Math.round(x) - HX, dy); return }
  c.save(); c.translate(Math.round(x), 0); c.scale(-1, 1); c.drawImage(spr, -HX, dy); c.restore()
}

// ── bubbles: what the agent does with the file, as a tiny animation ────────
type Bub = { ins: number; fill?: string; line?: string; inner?: string; glow?: string; tail?: boolean; ink: string; paper: string; acc: string; ok: string; hi: string }
const BUB: Record<string, Bub> = {
  round: { ins: 1, fill: '#ffffff', line: '#1a1410', tail: true, ink: '#1a1a22', paper: '#f6f2e6', acc: '#e05050', ok: '#3aa03a', hi: '#f0c040' },
  box: { ins: 2, fill: '#f8f8f8', line: '#3050a0', inner: '#90b0f0', ink: '#283048', paper: '#ffffff', acc: '#d04040', ok: '#30a050', hi: '#f8d048' },
  holo: { ins: 1, glow: 'rgba(64,200,255,0.22)', ink: '#9ff0ff', paper: 'rgba(64,200,255,0.28)', acc: '#ffb86b', ok: '#8affc0', hi: '#ffffff' },
}
function bubble(c: C2, style: string, icon: string, cx: number, bottom: number, t: number) {
  const b = BUB[style]!
  if (icon === 'sleep') return zzz(c, cx, bottom, t, style === 'holo' ? '#9ff0ff' : '#ffffff')
  const w = 12 + b.ins * 2, h = 9 + b.ins * 2, x = Math.round(cx - w / 2), y = Math.round(bottom - h - (b.tail ? 2 : 0))
  if (b.glow) R(c, x - 1, y - 1, w + 2, h + 2, b.glow)
  else {
    if (style === 'round') { R(c, x + 1, y, w - 2, h, b.line!); R(c, x, y + 1, w, h - 2, b.line!) } else R(c, x, y, w, h, b.line!)
    if (b.inner) { R(c, x + 1, y + 1, w - 2, h - 2, b.inner); R(c, x + 2, y + 2, w - 4, h - 4, b.fill!) } else R(c, x + 1, y + 1, w - 2, h - 2, b.fill!)
    if (b.tail) { R(c, cx - 1, y + h, 2, 1, b.line!); R(c, cx - 1, y + h + 1, 1, 1, b.line!) }
  }
  drawIcon(c, b, icon, x + b.ins, y + b.ins, t)
}
function zzz(c: C2, cx: number, bottom: number, t: number, col: string) {
  for (let i = 0; i < 3; i++) {
    const q = (t * 0.7 + i / 3) % 1, x = cx - 2 + q * 6, y = bottom - 3 - q * 9
    c.globalAlpha = 1 - q
    R(c, x, y, 3, 1, col); R(c, x + 1, y + 1, 1, 1, col); R(c, x, y + 2, 3, 1, col)
  }
  c.globalAlpha = 1
}
function page(c: C2, b: Bub, x: number, y: number) { R(c, x, y, 7, 9, b.ink); R(c, x + 1, y + 1, 5, 7, b.paper) }
function drawIcon(c: C2, b: Bub, icon: string, x: number, y: number, t: number) {
  const f = Math.floor
  switch (icon) {
    case 'read': { page(c, b, x + 2, y); const k = f(t * 2) % 3; R(c, x + 3, y + 2 + k * 2, 5, 1, b.hi); for (let i = 0; i < 3; i++) if (i !== k) R(c, x + 4, y + 2 + i * 2, 3, 1, b.ink); R(c, x + 10, y + 1 + k * 2, 2, 2, b.ink); break }
    case 'edit': { page(c, b, x, y); R(c, x + 2, y + 2, 3, 1, b.ink); R(c, x + 2, y + 6, 3, 1, b.ink); R(c, x + 2, y + 4, 4, 1, f(t * 1.5) % 2 ? b.ok : b.acc); const p = x + 7 + f(t * 4) % 3; R(c, p, y + 5, 1, 1, b.ink); R(c, p + 1, y + 4, 1, 1, b.hi); R(c, p + 2, y + 3, 1, 1, b.hi); R(c, p + 3, y + 2, 1, 1, b.acc); break }
    case 'write': { page(c, b, x + 2, y); const n = f(t * 2) % 4; for (let i = 0; i < n; i++) R(c, x + 4, y + 2 + i * 2, 3, 1, b.ink); if (f(t * 4) % 2) R(c, x + 4, y + 2 + Math.min(n, 2) * 2, 1, 1, b.acc); R(c, x + 10, y + 7, 1, 1, b.ok); R(c, x + 9, y + 8, 3, 1, b.ok); break }
    case 'grep': { R(c, x + 1, y + 1, 7, 8, b.ink); page(c, b, x + 3, y); const mx = x + 2 + Math.round((Math.sin(t * 3) + 1) * 2.5), my = y + 2; if (Math.sin(t * 3) > 0.7) R(c, x + 4, y + 4, 5, 1, b.hi); R(c, mx, my, 3, 1, b.acc); R(c, mx, my + 3, 3, 1, b.acc); R(c, mx - 1, my + 1, 1, 2, b.acc); R(c, mx + 3, my + 1, 1, 2, b.acc); R(c, mx + 3, my + 4, 1, 1, b.acc); R(c, mx + 4, my + 5, 1, 1, b.acc); break }
    case 'bash': { R(c, x, y, 12, 9, '#06140a'); for (let i = 0; i < 3; i++) R(c, x + 1, y + 1 + i * 2, 2 + ((i * 5 + f(t * 3) * 3) % 7), 1, '#40ff80'); if (f(t * 3) % 2) R(c, x + 1, y + 7, 2, 1, '#40ff80'); break }
    case 'web': { R(c, x + 3, y, 6, 1, b.ink); R(c, x + 2, y + 1, 8, 7, b.ink); R(c, x + 3, y + 1, 6, 7, b.paper); R(c, x + 3, y + 8, 6, 1, b.ink); R(c, x + 5 + f(t * 3) % 3 - 1, y + 1, 1, 7, b.acc); R(c, x + 3, y + 4, 6, 1, b.acc); break }
    case 'phone': { R(c, x + 2, y + 1, 2, 3, b.ink); R(c, x + 3, y + 3, 1, 3, b.ink); R(c, x + 2, y + 5, 2, 3, b.ink); if (f(t * 4) % 2) { R(c, x + 6, y + 3, 1, 3, b.acc); R(c, x + 8, y + 2, 1, 5, b.acc) } R(c, x + 10, y + 1, 1, 7, b.acc); break }
    case 'skill': case 'tool': { const k = f(t * 3) % 2; R(c, x + 4, y + 1 + k, 4, 2, b.ink); R(c, x + 5, y + 3 + k, 2, 5, b.ink); R(c, x + 3, y + 1 + k, 1, 1, b.hi); R(c, x + 8, y + 1 + k, 1, 1, b.hi); break }
    case 'think': { const n = f(t * 2) % 4; for (let i = 0; i < n; i++) R(c, x + 1 + i * 4, y + 4, 2, 2, b.ink); break }
    case 'coffee': { R(c, x + 3, y + 4, 6, 5, b.ink); R(c, x + 4, y + 4, 4, 4, b.paper); R(c, x + 4, y + 4, 4, 1, b.acc); R(c, x + 9, y + 5, 1, 2, b.ink); for (let i = 0; i < 2; i++) R(c, x + 4 + i * 2, y + ((f(t * 3) + i) % 2), 1, 3, b.hi); break }
    case 'rest': { const k = f(t * 2) % 2; R(c, x + 6, y + 1 + k, 1, 6, b.ink); R(c, x + 4, y + 6 + k, 3, 2, b.ink); R(c, x + 7, y + 1 + k, 3, 1, b.ink); R(c, x + 9, y + 2 + k, 1, 1, b.ink); break }
    case 'done': { for (const [dx, dy] of [[2, 5], [3, 6], [4, 7], [5, 6], [6, 5], [7, 4], [8, 3], [9, 2], [3, 5], [4, 6], [8, 2]] as const) R(c, x + dx, y + dy, 1, 1, b.ok); break }
  }
}

// action glyphs 5×5 in the info line, a banknote and a coin
const GLYPH: Record<string, string[]> = {
  clock: ['.###.', '#.#.#', '#.##.', '#...#', '.###.'],
  read: ['#####', '#...#', '#.#.#', '#...#', '#####'], edit: ['...##', '..##.', '.##..', '##...', '#....'],
  write: ['####.', '#..#.', '#..##', '#.###', '####.'], grep: ['.##..', '#..#.', '#..#.', '.###.', '....#'],
  bash: ['#....', '.#...', '#....', '.....', '..###'], web: ['.###.', '#.#.#', '#####', '#.#.#', '.###.'],
  phone: ['##...', '#....', '#....', '#..##', '.###.'], skill: ['..#..', '.###.', '#####', '.###.', '.#.#.'],
  tool: ['.#.#.', '.###.', '..#..', '..#..', '..#..'], think: ['.....', '.....', '#.#.#', '.....', '.....'],
  coffee: ['.#.#.', '.....', '####.', '#..##', '####.'], done: ['....#', '...#.', '#.#..', '.#...', '.....'],
  sleep: ['####.', '..#..', '.#...', '####.', '.....'], walk: ['..#..', '.###.', '#.#.#', '..#..', '.#.#.'],
  bad: ['#...#', '.#.#.', '..#..', '.#.#.', '#...#'], lift: ['#####', '#.#.#', '#.#.#', '#.#.#', '#####'], home: ['..#..', '.###.', '#####', '.#.#.', '.#.#.'], rest: ['..##.', '..#.#', '..#..', '###..', '##...'],
}
const ICON_COLOR: Record<string, string> = { clock: '#bfe3ff', home: '#e0a070', read: '#7fdcff', edit: '#ffd060', write: '#8af0a0', grep: '#ffa070', bash: '#60ff90', web: '#9ab8ff', phone: '#ff8ac8', skill: '#ffd060', tool: '#d8d8f0', think: '#d8d8f0', coffee: '#e0b080', done: '#8af0a0', sleep: '#a0a8d0', walk: '#d8d8f0', lift: '#d8d8f0', rest: '#c8a0ff' }
const DIM = 'rgba(235,235,250,0.45)'   // icons and figures of someone not at work
function glyph(c: C2, name: string, x: number, y: number, color = ICON_COLOR[name]!) {
  const g = GLYPH[name]
  if (!g) return
  R(c, x - 1, y - 1, 7, 7, 'rgba(10,10,20,0.55)')
  g.forEach((row, j) => { for (let i = 0; i < 5; i++) if (row[i] === '#') R(c, x + i, y + j, 1, 1, color) })
}
function banknote(c: C2, x: number, y: number) { R(c, x, y, 7, 5, '#1f6a32'); R(c, x + 1, y + 1, 5, 3, '#4cb860'); R(c, x + 3, y + 1, 1, 3, '#1f6a32'); R(c, x + 2, y + 2, 3, 1, '#1f6a32') }
function coin(c: C2, x: number, y: number) { R(c, x + 1, y, 2, 4, '#c08a10'); R(c, x, y + 1, 4, 2, '#c08a10'); R(c, x + 1, y + 1, 2, 2, GOLD) }

// ── furniture ──────────────────────────────────────────────────────────────
function drawChair(c: C2, r: Room, f: Fig, sx: number, sy: number) {
  const hy = sy - f.shin, tw = f.torso[0], bx = sx - Math.ceil(tw / 2) - 2, bh = Math.round(f.torso[1] * 0.7), L = r.line
  R(c, bx, hy - bh, 2, bh + 1, L || r.chair); if (L) R(c, bx + 1, hy - bh + 1, 1, bh, r.chair)
  box(c, bx, hy, f.thigh + Math.ceil(tw / 2) + 3, 2, r.chair, L)
  R(c, bx + 3, hy + 2, 1, sy - hy - 2, L || shade(r.chair, 0.7)); R(c, bx + 1, sy - 1, 6, 1, L || shade(r.chair, 0.7))
}
function screenFill(c: C2, x: number, y: number, w: number, h: number, icon: string, t: number) {
  if (icon === 'bash') { R(c, x, y, w, h, '#06140a'); for (let i = 0; i < h; i += 2) R(c, x + 1, y + i, Math.min(w - 2, 1 + ((i * 7 + Math.floor(t * 4)) % (w - 1))), 1, '#40e070'); return }
  R(c, x, y, w, h, icon === 'think' ? '#5a8aa0' : '#9fe4ff')
  for (let i = 1; i < h - 1; i += 2) R(c, x + 1, y + i, Math.min(w - 2, 2 + ((i * 5 + Math.floor(t * 3)) % Math.max(1, w - 3))), 1, '#2a6a8a')
}
function drawDesk(c: C2, r: Room, f: Fig, sx: number, sy: number, wide: boolean, on: boolean, icon: string, mug: boolean, stack: number, t: number) {
  const hy = sy - f.shin, dh = deskH(f), top = hy - dh, w = wide ? 40 : 28, x0 = sx + f.thigh - 2, L = r.line
  box(c, x0, top, w, 3, r.deskTop, L)
  R(c, x0, top + 3, w, 1, r.deskD)
  R(c, x0 + 1, top + 3, 2, sy - top - 3, r.deskD); R(c, x0 + w - 3, top + 3, 2, sy - top - 3, r.deskD)
  const mh = Math.max(5, Math.min(11, dh + 3)), mw = mh + 5, mx = x0 + Math.round(w * 0.42), my = top - mh - 1
  box(c, mx, my, mw, mh, r.pc, L || r.pcD)
  if (on) screenFill(c, mx + 1, my + 1, mw - 2, mh - 2, icon, t); else R(c, mx + 1, my + 1, mw - 2, mh - 2, r.off)
  R(c, mx + Math.floor(mw / 2) - 1, top - 1, 2, 1, r.pcD)
  R(c, x0 + 2, top - 1, 6, 1, r.pcD)
  if (mug) { R(c, x0 + w - 6, top - 3, 3, 3, '#f0f0f0'); R(c, x0 + w - 3, top - 2, 1, 1, '#f0f0f0') }
  for (let i = 0; i < Math.min(8, stack); i++) R(c, x0 + w - 13, top - 2 - i * 2, 7, 2, i % 2 ? '#c09020' : '#e0b030')
  return { mx: mx + mw / 2, my: my + mh / 2 }
}
function drawCouch(c: C2, r: Room, x: number, base: number, w: number) {
  const col = r.couch, L = r.line
  box(c, x, base - 14, w, 8, col, L); box(c, x, base - 7, w, 6, shade(col, 0.85), L)
  box(c, x - 3, base - 11, 5, 10, shade(col, 0.92), L); box(c, x + w - 2, base - 11, 5, 10, shade(col, 0.92), L)
  R(c, x + 2, base - 1, 2, 2, L || INK); R(c, x + w - 4, base - 1, 2, 2, L || INK)
}
function drawCooler(c: C2, r: Room, x: number, floorY: number) {
  box(c, x + 1, floorY - 22, 6, 7, '#9fd8ff', r.line)
  box(c, x, floorY - 15, 8, 15, r.cooler, r.line || shade(r.cooler, 0.7)); R(c, x + 2, floorY - 11, 2, 1, '#3a6ab0')
}
// a little sign with a house on the desk of someone gone home
function homeSign(c: C2, x: number, deskTop: number) {
  box(c, x, deskTop - 8, 9, 8, '#f4f1e8', '#5a4a3a')
  GLYPH.home!.forEach((row, j) => { for (let i = 0; i < 5; i++) if (row[i] === '#') R(c, x + 2 + i, deskTop - 7 + j, 1, 1, '#a0502a') })
}
// an epic floor's stages as a row of lamps: green passed, blue next (it pulses), red rejected, grey ahead
const STAGE_COLOR: Record<string, string> = { passed: '#2f9e57', next: '#3a7bd5', rejected: '#c8403a', ahead: '#4a4a58' }
const SEG = { w: 30, h: 9, gap: 2 }
// room: the width the track may take; a long one (the landed packages of a big epic) narrows its plates to fit.
// ponytail: plates stop narrowing at 14 px, so past ~30 landed packages the track runs into the wall: group them then
function stageTrack(c: C2, labels: Array<{ x: number; y: number; text: string; px: number; color?: string; w?: number; stroke?: boolean; maxw?: number }>, track: Array<[string, string]>, x: number, y: number, t: number, room = Infinity): number {
  const w = Math.max(14, Math.min(SEG.w, Math.floor(room / track.length) - SEG.gap))
  track.forEach(([text, state], i) => {
    const sx = x + i * (w + SEG.gap)
    box(c, sx, y, w, SEG.h, STAGE_COLOR[state] ?? STAGE_COLOR.ahead!, '#1b1d2e')
    if (state === 'next') { c.globalAlpha = 0.25 + 0.25 * Math.sin(t * 4); R(c, sx + 1, y + 1, w - 2, SEG.h - 2, '#ffffff'); c.globalAlpha = 1 }
    labels.push({ x: sx + w / 2, y: y + 1.6, text, px: 3.3, color: state === 'ahead' ? 'rgba(235,235,250,0.55)' : '#ffffff', w: 600, stroke: false, maxw: w - 2 })
  })
  return track.length * (w + SEG.gap)
}

function whiteboard(c: C2, x: number, y: number, w: number, h: number) {
  box(c, x, y, w, h, '#f4f4ee', '#8a8a96'); R(c, x + 1, y + 1, w - 2, 1, '#ffffff')
  R(c, x + 2, y + h, w - 4, 1, '#6a6a76'); R(c, x + w - 9, y + h - 1, 3, 1, '#2445a8'); R(c, x + w - 5, y + h - 1, 3, 1, '#c03040')
}
function liftDoors(c: C2, x: number, top: number, open: boolean, coming: boolean, t: number) {
  const y = top + 20, w = 16, h = 26
  box(c, x, y, w, h, '#2a2a34', '#4a4a56')
  if (open) { R(c, x + 2, y + 2, w - 4, h - 2, '#fff3c0'); R(c, x + 1, y + 1, 2, h - 1, '#9aa2ac'); R(c, x + w - 3, y + 1, 2, h - 1, '#9aa2ac') }
  else { R(c, x + 1, y + 1, w / 2 - 1, h - 1, '#b8c0c8'); R(c, x + w / 2, y + 1, w / 2 - 1, h - 1, '#a8b0b8'); R(c, x + w / 2 - 1, y + 1, 1, h - 1, '#6a7280') }
  R(c, x + 5, y - 4, 6, 3, '#1a1a22')
  R(c, x + 7, y - 3, 2, 1, open ? '#8affc0' : coming && Math.floor(t * 4) % 2 ? '#ffb040' : '#3a3a44')
}
function plant(c: C2, x: number, floorY: number) {
  R(c, x, floorY - 6, 6, 6, '#a0603a'); R(c, x, floorY - 6, 6, 1, '#7a4428')
  R(c, x + 1, floorY - 13, 4, 7, '#3a8a4a'); R(c, x - 1, floorY - 11, 3, 4, '#4a9a4a'); R(c, x + 4, floorY - 12, 3, 4, '#4a9a4a'); R(c, x + 2, floorY - 15, 2, 2, '#5aaa5a')
}

// ── light ──────────────────────────────────────────────────────────────────
type Light = { kind: 'screen' | 'lamp'; x: number; y: number; bash?: boolean }
function glow(c: C2, x: number, y: number, r: number, col: string, a: number) {
  c.fillStyle = col
  for (const k of [1, 0.66, 0.36]) { c.globalAlpha = a; c.beginPath(); c.arc(x, y, r * k, 0, Math.PI * 2); c.fill() }
  c.globalAlpha = 1
}
function lighting(c: C2, w: number, h: number, tm: string, lights: Light[], room: Room) {
  const T = TIMES[tm]!
  if (T.tint) { c.globalCompositeOperation = 'multiply'; c.fillStyle = T.tint; c.fillRect(0, 0, w, h) }
  c.globalCompositeOperation = 'lighter'
  for (const l of lights) {
    if (l.kind === 'screen') glow(c, l.x, l.y, tm === 'night' ? 8 : 6, l.bash ? '#20a050' : '#2a8ab0', tm === 'night' ? 0.1 : 0.05)
    if (l.kind === 'lamp' && (T.lamps || (room.lampsAlways && tm !== 'day'))) glow(c, l.x, l.y, 12, '#ffa040', 0.07)
  }
  c.globalCompositeOperation = 'source-over'
}
function sky(c: C2, w: number, h: number, tm: string, t: number) {
  const T = TIMES[tm]!
  R(c, 0, 0, w, h * 0.5, T.sky[0]); R(c, 0, h * 0.5, w, h * 0.5, T.sky[1])
  if (T.sun) { const x = w * T.sun[0], y = h * T.sun[1]; R(c, x - 3, y - 3, 7, 7, tm === 'evening' ? '#ffb070' : '#fff3b0') }
  if (T.moon) { R(c, w * 0.8, 4, 6, 6, '#f0f0d8'); R(c, w * 0.8 + 3, 4, 3, 3, T.sky[0]) }
  if (tm === 'night') for (let i = 0; i < 26; i++) if ((i * 7 + Math.floor(t * 1.2)) % 6) R(c, (i * 37) % w, 2 + (i * 11) % Math.max(4, h - 4), 1, 1, '#ffffff')
}

// ── layout ─────────────────────────────────────────────────────────────────
// a cell is 48×61: two info lines (what it does; model and money) · bubble · desk · name plate · slab.
// A cell's `top` is where its scene starts, after the first 6 px of the info lines.
const FL = { pad: 6, shaft: 19, plaque: 54, cw: 48, rowH: 61, info: 6, roof: 24, ground: 10, minCols: 8, maxCols: 12, strip: 23 }
const RACK = { w: 24, h: 36, shelf: 5, steps: [0, 60, 180, 300, 600, 900, 1800] }   // a shelf lights up at each age, in seconds
const shellColor = (s: Shell) => s.status === 'failed' ? '#e05a5a' : s.status === 'completed' ? '#4ade80' : s.status === 'running' ? GOLD : '#8a8fb0'
type Geo = { x: number; top: number; sx: number; sy: number; wide?: boolean }
type FloorGeo = { def: FloorDef; y: number; h: number; rows: number }
type Layout = { key: string; f: Fig; srv: number; cols: number; x0: number; width: number; height: number; roof: number; floors: FloorGeo[]; cells: Map<string, Geo>; people: Map<string, Geo>; boss: Geo; lobby: number; couchX: number; couchW: number; seats: number }

function layout(b: { heads: Person[]; floors: FloorDef[] }, room: Room, key: string, shells: boolean): Layout {
  const f = FIGS[room.fig]!
  const cols = Math.max(FL.minCols, Math.min(FL.maxCols, ...b.floors.map(fl => fl.cells.length)))
  const x0 = FL.pad + FL.shaft + FL.plaque, width = x0 + cols * FL.cw + FL.pad
  // a task of several sessions gets a line under the roof saying which one is at work now
  const roof = FL.roof + (b.heads.length ? 7 : 0)
  let y = roof
  const bx = x0 + Math.floor((cols - 2) / 2) * FL.cw
  const boss: Geo = { x: bx, top: y + FL.info, sx: bx + 14, sy: y + FL.info + 46, wide: true }
  // a cell is known by its desk's type, the one sitting there by the agent's id; trips and mail look up the person
  const cells = new Map<string, Geo>(), people = new Map<string, Geo>()
  // the heads of started sessions: right of the boss, then left, as many as the room holds
  b.heads.forEach((h, i) => {
    const k = Math.floor(i / 2) + 1, x = i % 2 === 0 ? bx + 2 * FL.cw + (k - 1) * FL.cw : bx - k * FL.cw
    if (x >= x0 && x + FL.cw <= x0 + cols * FL.cw) people.set(h.key, { x, top: y + FL.info, sx: x + 12, sy: y + FL.info + 46 })
  })
  y += FL.rowH
  // the server room exists while there is something to show, right under the cabinet: no scrolling to it
  const srv = shells ? y : 0
  if (shells) y += FL.rowH
  const floors: FloorGeo[] = []
  for (const def of b.floors) {
    // a floor whose free-lancers all went home still keeps one row of empty desks; a folded one is a band
    const rows = def.strip ? 0 : Math.max(1, Math.ceil(def.cells.length / cols))
    const h = def.strip ? FL.strip : rows * FL.rowH
    floors.push({ def, y, h, rows })
    def.cells.forEach((cell, i) => {
      const top = y + Math.floor(i / cols) * FL.rowH + FL.info, x = x0 + (i % cols) * FL.cw
      const geo = { x, top, sx: x + 12, sy: top + 46 }
      cells.set(cell.key, geo)
      if (cell.person) people.set(cell.person.key, geo)
    })
    y += h
  }
  const lobby = y + FL.info
  const couchX = x0 + 60, couchW = width - FL.pad - 12 - couchX
  return { key, f, srv, cols, x0, width, height: lobby - FL.info + FL.rowH + FL.ground, roof, floors, cells, people, boss, lobby, couchX, couchW, seats: Math.max(1, Math.floor((couchW - 4) / 22)) }
}

// ── the view ───────────────────────────────────────────────────────────────
type Label = { x: number; y: number; text: string; px: number; color?: string; w?: number; align?: CanvasTextAlign; maxw?: number; stroke?: boolean; display?: boolean }
type Flight = { key: string; t0: number; cost: number }

export type FloorsView = { update(st: Studio): void; reset(): void; setStyle(office: string, time: string): void; picture(zoom?: number): HTMLCanvasElement }

export function mountFloors(scene: HTMLCanvasElement, overlay: HTMLCanvasElement, onError: (e: unknown) => void): FloorsView {
  const ctx = scene.getContext('2d')!, octx = overlay.getContext('2d')!
  let st: Studio | null = null
  let style = { office: 'cozy', time: 'clock' }
  let L: Layout | null = null
  let built: Built | null = null
  const mem = new Map<string, Memory>()
  const seats = { couch: [] as Array<string | null>, cooler: [null, null] as Array<string | null> }
  const flights: Flight[] = []

  function sizeOverlay() {
    const r = scene.getBoundingClientRect(), dpr = window.devicePixelRatio || 1
    overlay.width = Math.round(r.width * dpr); overlay.height = Math.round(r.height * dpr)
  }
  new ResizeObserver(sizeOverlay).observe(scene)

  function relayout(now: number) {
    if (!st) return
    built = building(st, now)
    const room = OFFICES[style.office] ?? OFFICES.cozy!
    const shells = !!st.shells?.length
    const key = style.office + '|' + shells + '|' + built.heads.map(h => h.key).join(',') + '|' + built.floors.map(f => f.name + ':' + f.cells.map(c => c.key + '=' + (c.person?.key ?? '')).join(',')).join('/')
    if (L && L.key === key) {
      // the same seats, fresh people: money and who went home change without a new layout
      L.floors.forEach((fl, i) => { fl.def = built!.floors[i]! })
      return
    }
    L = layout(built, room, key, shells)
    if (seats.couch.length !== L.seats) seats.couch = Array.from({ length: L.seats }, (_, i) => seats.couch[i] ?? null)
    if (scene.width !== L.width || scene.height !== L.height) { scene.width = L.width; scene.height = L.height; scene.style.aspectRatio = `${L.width} / ${L.height}`; sizeOverlay() }
  }

  function takeSeat(kind: 'couch' | 'cooler', key: string): number {
    const list = seats[kind]
    const mine = list.indexOf(key)
    if (mine >= 0) return mine
    const i = list.indexOf(null)
    if (i >= 0) list[i] = key
    return i
  }
  function leaveSeat(key: string) {
    for (const list of [seats.couch, seats.cooler]) { const i = list.indexOf(key); if (i >= 0) list[i] = null }
  }

  // the emptier of the two cabins; a trip keeps it, however the people around reshuffle
  function freeLane(t: number): number {
    const riders = [0, 0]
    for (const { trip } of mem.values()) if (trip && t - trip.t0 < WALK && rides(trip)) riders[trip.lane]!++
    return riders[1]! < riders[0]! ? 1 : 0
  }

  // remember where everyone is; a change of place starts a trip
  function track(p: Person, d: Doing, now: number, t: number): Memory {
    let m = mem.get(p.key)
    const lounge = (pl: Place) => pl === 'couch' || pl === 'cooler'
    if (!m) {
      m = { place: d.place, seat: lounge(d.place) ? takeSeat(d.place as 'couch' | 'cooler', p.key) : -1, finished: !!p.agent?.finished }
      // a fresh free-lancer walks in from the street; a desk's owner is already at the desk
      if (p.agent && !p.desk && d.place === 'desk' && now - p.agent.startedAt < WALK * 1000) m.trip = { from: 'gone', to: 'desk', t0: p.agent.startedAt / 1000, fromSeat: -1, toSeat: -1, lane: freeLane(t) }
      mem.set(p.key, m)
    } else if (m.place !== d.place && (!m.trip || t - m.trip.t0 >= WALK)) {
      const fromSeat = m.seat
      leaveSeat(p.key)
      const toSeat = lounge(d.place) ? takeSeat(d.place as 'couch' | 'cooler', p.key) : -1
      m.trip = { from: m.place, to: d.place, t0: t, fromSeat, toSeat, lane: freeLane(t) }
      m.place = d.place; m.seat = toSeat
    }
    if (m.trip && t - m.trip.t0 >= WALK) m.trip = undefined
    if (p.agent?.finished && !m.finished) flights.push({ key: p.key, t0: t, cost: p.agent.cost })
    m.finished = !!p.agent?.finished
    return m
  }

  function render(now: number) {
    if (!st || !built || !L) return []
    const t = now / 1000, room = OFFICES[style.office] ?? OFFICES.cozy!, tm = timeNow(style.time), f = L.f
    const labels: Label[] = [], lights: Light[] = [], bubbles: Array<{ icon: string; x: number; y: number }> = []
    const { x0, width, height, lobby } = L
    const xL = FL.pad + FL.shaft, xR = width - FL.pad
    const c = ctx
    c.clearRect(0, 0, width, height)
    sky(c, width, height, tm, t)
    R(c, FL.pad - 3, L.roof - 5, width - 2 * FL.pad + 6, height - L.roof - FL.ground + 5, room.facade)
    R(c, FL.pad - 4, L.roof - 6, width - 2 * FL.pad + 8, 2, shade(room.facade, 0.7))
    box(c, x0 + 20, 4, L.cols * FL.cw - 40, 13, '#14152a', GOLD)
    for (let i = 0; i < 3; i++) banknote(c, x0 + 24 + i, 9 - i)
    R(c, 0, height - FL.ground, width, FL.ground, '#3a3a46'); R(c, 0, height - FL.ground, width, 1, '#55556a')

    // who is doing what
    const everyone: Person[] = [built.boss, ...built.heads, ...built.floors.flatMap(fl => fl.cells.flatMap(cell => cell.person ? [cell.person] : []))]
    // a copy sent home loses its cell and never reaches track: its seat is freed here
    const here = new Set(everyone.map(p => p.key))
    for (const list of [seats.couch, seats.cooler]) list.forEach((k, i) => { if (k && !here.has(k)) list[i] = null })
    const now_ = new Map<string, Doing>(), memo = new Map<string, Memory>()
    for (const p of everyone) { const d = doing(p, st, now); now_.set(p.key, d); memo.set(p.key, track(p, d, now, t)) }
    // «в работе» как на сервере и в списке сессий: незавершённые агенты и занятый руководитель
    const heads = st.sessions ?? []
    const working = st.agents.filter(a => a.isWorking).length + heads.filter(h => h.isBusy).length
    labels.push({ x: width / 2, y: 7.5, text: st.office.title || 'AGENT OFFICE', px: 5, color: st.office.accent ?? GOLD, display: true, stroke: false })
    labels.push({ x: width / 2, y: 18, text: `в работе ${working} · агентов ${st.agents.length}`, px: 4.2, color: '#ffffff', w: 600 })
    // how long anyone worked at the left edge: joined across the head, agents and background commands
    const worked = workedNow(st, now)
    if (worked) {
      glyph(c, 'clock', xL + 2, 18.3, TIME_COLOR)
      labels.push({ x: xL + 9, y: 18, text: span(worked), px: 4.2, color: TIME_COLOR, align: 'left', w: 600 })
    }
    // the session's total at the right edge, after a banknote
    const total = money(st.cost)
    banknote(c, xR - 3 - total.length * 2.6 - 9, 18.3)
    labels.push({ x: xR - 2, y: 18, text: total, px: 4.2, color: GOLD, align: 'right', w: 600 })
    if (heads.length > 1) {
      // which session is at work now: the busy one that wrote last, else the one that wrote last
      const latest = (hs: Head[]) => hs.reduce((a, b) => (b.at > a.at ? b : a))
      const cur = latest(heads.filter(h => h.isBusy).length ? heads.filter(h => h.isBusy) : heads)
      labels.push({ x: width / 2, y: 24.5, text: `сейчас: ${markOf(st, heads.indexOf(cur) + 1)}${cur.title} · сессий ${heads.length}`, px: 3.6, color: 'rgba(235,235,250,0.85)', maxw: width - 2 * FL.pad - 8 })
    }

    // the producer's office: panorama window, carpet, board
    const doorTops: number[] = [], P = L.boss.top
    R(c, xL, P - FL.info, xR - xL, 46 + FL.info, shade(room.wall, 0.93))
    const wx = x0 + 6, ww = L.cols * FL.cw - 12, night = tm === 'night' || tm === 'evening'
    box(c, wx, P + 5, ww, 30, TIMES[tm]!.sky[1], shade(room.wall, 0.6)); R(c, wx + 1, P + 6, ww - 2, 12, TIMES[tm]!.sky[0])
    for (let i = 1; i < 6; i++) R(c, wx + Math.round(ww * i / 6), P + 5, 1, 30, shade(room.wall, 0.6))
    for (let i = 0; i < ww - 4; i += 7) { const h = 4 + (i * 13) % 11; R(c, wx + 2 + i, P + 34 - h, 6, h, night ? '#141a30' : '#8aa0b8'); if (tm === 'night' && (i / 7) % 3 === 1) R(c, wx + 4 + i, P + 30 - h / 2, 1, 1, '#ffd27a') }
    R(c, xL, P + 46, xR - xL, 7, shade(room.floor, 0.8)); R(c, x0 + 20, P + 47, L.cols * FL.cw - 40, 5, '#8a2a3a'); R(c, xL, P + 46, xR - xL, 1, room.line || shade(room.floor, 0.6))
    R(c, xL, P + 53, xR - xL, 2, room.slab)
    doorTops.push(P)
    const arrived = flights.filter(fl => t - fl.t0 >= 1.4).length
    whiteboard(c, xL + 2, P - FL.info + 2, FL.plaque - 6, 19)
    labels.push({ x: xL + 4, y: P - FL.info + 3.3, text: 'КАБИНЕТ', px: 3.4, color: '#2445a8', w: 600, align: 'left', stroke: false })
    const tokens = st.office.epic?.context, limit = st.office.epic?.limit
    const kilo = (n: number) => Math.round(n / 1000) + 'k'
    labels.push({ x: xL + 4, y: P - FL.info + 8.6, text: tokens && limit ? `контекст ${kilo(tokens)}/${kilo(limit)}` : 'сдано работ: ' + arrived, px: 3.1, color: '#2a2a33', align: 'left', maxw: FL.plaque - 10, stroke: false })
    labels.push({ x: xL + 4, y: P - FL.info + 13.8, text: money(st.cost) + ' всего', px: 3.1, color: '#1f7a3a', w: 600, align: 'left', stroke: false })

    // floors
    for (const fl of L.floors) {
      const people = fl.def.cells.flatMap(cell => cell.person && !cell.person.waiting ? [cell.person] : [])
      const sum = people.reduce((s, p) => s + (p.agent?.cost ?? 0), 0)
      for (let r = 0; r < fl.rows; r++) {
        const top = fl.y + r * FL.rowH + FL.info
        R(c, xL, top - FL.info, xR - xL, 46 + FL.info, room.wall)
        R(c, x0, top - FL.info, xR - x0, 8 + FL.info, room.wall2)
        R(c, xL, top + 46, xR - xL, 7, room.floor); R(c, xL, top + 46, xR - xL, 1, room.line || shade(room.floor, 0.7))
        R(c, xL, top + 53, xR - xL, 2, room.slab)
        for (let k = 1; k < L.cols; k++) R(c, x0 + k * FL.cw, top + 8, 1, 38, shade(room.wall, 0.9))
        doorTops.push(top)
        if (r === 0) plant(c, xL + 36, top + 46)
      }
      const busyHere = people.filter(p => now_.get(p.key)!.work).length
      const second = fl.def.note ?? `в деле ${busyHere}` + (fl.def.home ? ` · дома ${fl.def.home}` : '')
      // a folded floor: a band with its stages and what the floor says, and no desks; a package not dispatched yet is dark
      const track = fl.def.track
      if (fl.def.strip) {
        const dark = !!track && track.every(([, s]) => s === 'ahead')
        R(c, xL, fl.y, xR - xL, fl.h - 2, dark ? shade(room.wall2, 0.55) : room.wall2); R(c, xL, fl.y + fl.h - 2, xR - xL, 2, room.slab)
      }
      whiteboard(c, xL + 2, fl.y + 2, FL.plaque - 6, 19)
      labels.push({ x: xL + 4, y: fl.y + 3.3, text: fl.def.name, px: 3.4, color: '#2445a8', w: 600, align: 'left', maxw: FL.plaque - 10, stroke: false })
      if (fl.def.strip) {
        // the band keeps room for what it says after the track
        const used = track ? stageTrack(c, labels, track, x0 + 4, fl.y + 6, t, L.cols * FL.cw - 80) : 0
        labels.push({ x: x0 + 8 + used, y: fl.y + 8.2, text: second, px: 3.6, color: 'rgba(245,245,255,0.92)', w: 600, align: 'left', maxw: L.cols * FL.cw - used - 12, stroke: false })
        continue
      }
      // a floor at work keeps its track on the wall, right of the desks
      if (track) stageTrack(c, labels, track, xR - track.length * (SEG.w + SEG.gap) - 4, fl.y + 2, t)
      labels.push({ x: xL + 4, y: fl.y + 8.6, text: second, px: 3.1, color: '#2a2a33', align: 'left', maxw: FL.plaque - 10, stroke: false })
      labels.push({ x: xL + 4, y: fl.y + 13.8, text: money(sum), px: 3.3, color: '#1f7a3a', w: 600, align: 'left', stroke: false })
    }
    // server room: a rack for each background command, or a shelf for each once the racks run out
    if (L.srv) {
      const ST = L.srv + FL.info, shells = st.shells ?? [], wall = shade(room.wall, 0.26), floor = shade(room.floor, 0.42), { w: rw, h: rh, shelf, steps } = RACK
      const shelves = steps.length, shelfY = (ry: number, k: number) => ry + 2 + (shelves - 1 - k) * shelf
      R(c, xL, L.srv, xR - xL, 46 + FL.info, wall)
      R(c, x0, L.srv, xR - x0, 8 + FL.info, shade(wall, 0.78))
      R(c, xL, ST + 46, xR - xL, 7, floor); R(c, xL, ST + 46, xR - xL, 1, shade(floor, 0.7))
      R(c, xL, ST + 53, xR - xL, 2, room.slab)
      doorTops.push(ST)
      const runs = shells.filter(s => s.status === 'running')
      whiteboard(c, xL + 2, L.srv + 2, FL.plaque - 6, 19)
      labels.push({ x: xL + 4, y: L.srv + 3.3, text: 'СЕРВЕРНАЯ', px: 3.4, color: '#2445a8', w: 600, align: 'left', stroke: false })
      labels.push({ x: xL + 4, y: L.srv + 8.6, text: 'идёт ' + runs.length, px: 3.1, color: '#2a2a33', align: 'left', stroke: false })
      if (runs.length) labels.push({ x: xL + 4, y: L.srv + 13.8, text: 'дольше всех ' + span(now - Math.min(...runs.map(s => s.start))), px: 3.1, color: '#1f7a3a', w: 600, align: 'left', maxw: FL.plaque - 10, stroke: false })
      const ageOf = (s: Shell) => ((s.end || now) - s.start) / 1000
      const dense = shells.length > L.cols, cap = L.cols * shelves, hidden = shells.length - cap + 1
      if (dense) {
        const bad = shells.filter(s => s.status === 'failed').length, done = shells.filter(s => s.status === 'completed').length
        labels.push({ x: x0 + 2, y: ST - 5, text: `идёт ${runs.length}` + (bad ? ` · упало ${bad}` : '') + (done ? ` · готово ${done}` : '') + (hidden > 1 ? ` · ещё ${hidden} не поместилось` : ''), px: 3.6, color: '#ffffff', align: 'left', w: 600 })
      }
      for (let i = 0; i < L.cols; i++) {
        const x = x0 + i * FL.cw, rx = x + 12, ry = ST + 10, single = dense ? undefined : shells[i]
        box(c, rx, ry, rw, rh, '#14152a', '#44486a')
        // a single rack fills from the bottom with age; a dense one has a shelf per command, as wide as it is old
        for (let k = 0; k < shelves; k++) {
          const y = shelfY(ry, k), n = i * shelves + (shelves - 1 - k), s = dense ? shells[n] : single
          R(c, rx + 3, y, rw - 6, 3, '#23253a')
          if (!s || (!dense && s.status === 'running' && ageOf(s) < steps[k]!)) continue
          if (dense && hidden > 1 && n === cap - 1) { labels.push({ x: rx + rw / 2, y: y - 0.6, text: '+' + hidden, px: 3.1, color: GOLD, w: 600 }); continue }
          const share = dense && s.status === 'running' ? Math.min(1, Math.log(1 + ageOf(s) / 30) / Math.log(121)) : 1
          R(c, rx + 3, y, Math.max(2, Math.round((rw - 6) * share)), 3, shade(shellColor(s), 0.72))
          if (s.status === 'running' && (Math.floor(t * 3) + n) % 4 !== 0) R(c, rx + rw - 5, y + 1, 1, 1, '#ffffff')
        }
        if (!single) continue
        const running = single.status === 'running', tint = running ? TIME_COLOR : shellColor(single)
        if (running) lights.push({ kind: 'screen', x: rx + rw / 2, y: ry + rh / 2, bash: true })
        glyph(c, running ? 'clock' : single.status === 'completed' ? 'done' : 'bad', x + 2, ST - 4.5, tint)
        labels.push({ x: x + 9, y: ST - 5, text: span(ageOf(single) * 1000), px: 3.6, color: tint, align: 'left', w: 600 })
        labels.push({ x: x + FL.cw / 2, y: ST + 47.2, text: single.name, px: 3.1, color: running ? '#ffffff' : 'rgba(235,235,250,0.62)', maxw: FL.cw - 3 })
      }
    }
    // lobby
    R(c, xL, lobby - FL.info, xR - xL, 46 + FL.info, room.wall)
    R(c, xL, lobby - FL.info, xR - xL, 8 + FL.info, room.wall2)
    R(c, xL, lobby + 46, xR - xL, 9, room.floor); R(c, xL, lobby + 46, xR - xL, 1, room.line || shade(room.floor, 0.7))
    doorTops.push(lobby)
    box(c, xL + 4, lobby + 11, 14, 5, '#1b1d2e', GOLD)
    labels.push({ x: xL + 11, y: lobby + 11.6, text: 'ХОЛЛ', px: 3, color: GOLD, w: 600, stroke: false })
    // lift shaft and the pneumatic tube
    R(c, FL.pad, L.roof - 2, FL.shaft, lobby + 46 - L.roof + 2, '#0e0e16')
    R(c, FL.pad + 7, L.roof - 2, 1, lobby + 46 - L.roof + 2, '#24242e')
    R(c, FL.pad + 15, L.roof - 2, 3, lobby + 46 - L.roof + 2, '#2a4a5a'); R(c, FL.pad + 16, L.roof - 2, 1, lobby + 46 - L.roof + 2, '#3a6a7a')

    // trips: corridor → cabin → lobby and back; positions follow the phases
    const cx = L.couchX, cw = L.couchW, SHAFT_X = xL + 10, CORR = 6
    const spot = (pl: Place, seat: number) => pl === 'gone' ? x0 + 9 : pl === 'cooler' ? (seat < 0 ? x0 + 46 : x0 + 38 + seat * 9) : (seat < 0 ? cx + cw - 10 : cx + 6 + seat * 22)
    type Walker = { p: Person; x: number; y: number; flip: boolean; fade?: number }
    type Car = { lane: number; y: number; rider?: Person; open?: boolean; door?: number; toward?: number }
    const walkers: Walker[] = [], cars: Car[] = []
    let doorOpen = false
    const frame = Math.floor(t * 7) % 4
    everyone.forEach(p => {
      const m = memo.get(p.key)!, trip = m.trip, cell = L!.people.get(p.key)
      if (!trip || !cell) return
      const q = Math.min(1, (t - trip.t0) / WALK)
      const toDesk = trip.to === 'desk', fromDesk = trip.from === 'desk'
      if (!toDesk && !fromDesk) {   // inside the lobby: seat to seat or to the street
        const a = spot(trip.from, trip.fromSeat), b = spot(trip.to, trip.toSeat)
        walkers.push({ p, x: a + (b - a) * q, y: lobby + 46 + CORR, flip: b < a, fade: trip.to === 'gone' && q > 0.85 ? (1 - q) / 0.15 : undefined })
        if (trip.to === 'gone' && q > 0.6) doorOpen = true
        return
      }
      const down = fromDesk, ph = tripPhase(down, q), k = ph.k, lane = trip.lane
      const place = down ? trip.to : trip.from, seat = down ? trip.toSeat : trip.fromSeat, sp = spot(place, seat)
      const rowCar = cell.top + 33, lobbyCar = lobby + 33
      if (ph.name === 'floor') {
        const from = cell.sx - 3, to = SHAFT_X, qq = down ? k : 1 - k
        walkers.push({ p, x: from + (to - from) * qq, y: cell.sy + CORR * Math.min(1, qq * 8), flip: down })
        if (down && k > 0.3) cars.push({ lane, y: lobbyCar + (rowCar - lobbyCar) * Math.min(1, (k - 0.3) / 0.65), toward: cell.top })
        // the cabin arrives: doors open, the rider steps out, then it goes back down
        if (!down && k < 0.25) cars.push({ lane, y: rowCar, open: true, door: cell.top })
        else if (!down && k < 0.7) cars.push({ lane, y: rowCar + (lobbyCar - rowCar) * ((k - 0.25) / 0.45) })
      } else if (ph.name === 'board') {
        cars.push({ lane, y: down ? rowCar : lobbyCar, rider: p, open: true, door: down ? cell.top : lobby })
      } else if (ph.name === 'ride') {
        cars.push({ lane, y: down ? rowCar + (lobbyCar - rowCar) * k : lobbyCar + (rowCar - lobbyCar) * k, rider: p, toward: down ? lobby : cell.top })
      } else {
        const qq = down ? k : 1 - k
        if (down && k < 0.25) cars.push({ lane, y: lobbyCar, open: true, door: lobby })   // the cabin arrives: doors open, the rider steps out
        const w: Walker = { p, x: SHAFT_X + (sp - SHAFT_X) * qq, y: lobby + 46 + CORR, flip: down ? sp < SHAFT_X : sp > SHAFT_X }
        if (place === 'gone') { if (down ? k > 0.7 : k < 0.3) doorOpen = true; if (down && k > 0.85) w.fade = (1 - k) / 0.15; if (!down && k < 0.15) w.fade = k / 0.15 }
        walkers.push(w)
      }
    })
    const open = new Set(cars.filter(k => k.open).map(k => k.door))
    const coming = new Set(cars.filter(k => k.toward != null).map(k => k.toward))
    for (const top of doorTops) liftDoors(c, xL + 2, top, open.has(top), coming.has(top), t)

    // lobby: street door, cooler, couch and those already in their seats
    box(c, x0 + 2, lobby + 16, 14, 30, doorOpen ? '#07070d' : room.door, room.line || shade(room.door, 0.7))
    if (!doorOpen) R(c, x0 + 12, lobby + 31, 2, 2, GOLD)
    drawCooler(c, room, x0 + 22, lobby + 46)
    drawCouch(c, room, cx, lobby + 46, cw)
    let lounging = 0, atCooler = 0, overflow = 0
    // a name under everyone in the lounge, on two staggered lines so neighbours do not collide; free-lancers are tagged
    const tag = (p: Person, x: number, row: number, align: CanvasTextAlign) =>
      labels.push({ x, y: lobby + 47.2 + row * 4.2, text: p.desk ? p.name : p.name + ' · фриланс', px: 3.4, color: p.desk ? '#ffffff' : '#d8dcff', align, maxw: 43 })
    for (const p of everyone) {
      const m = memo.get(p.key)!
      if (m.trip && t - m.trip.t0 < WALK) continue
      if (m.place === 'cooler') {
        atCooler++
        if (m.seat < 0) { overflow++; continue }
        blit(c, sprite(f, 'standDrink', 0, p.grade, p.look, false), x0 + 38 + m.seat * 9, lobby + 46, true, true)
        tag(p, x0 + 44, m.seat, 'right')
      } else if (m.place === 'couch') {
        lounging++
        if (m.seat < 0) { overflow++; continue }
        blit(c, sprite(f, 'sit', 0, p.grade, p.look, false), cx + 6 + m.seat * 22, lobby + 48 - f.shin, false, false)
        tag(p, cx + 13 + m.seat * 22, m.seat % 2, 'center')
      }
    }
    if (atCooler) bubbles.push({ icon: 'coffee', x: x0 + 42, y: lobby + 21 })
    if (lounging) bubbles.push({ icon: 'rest', x: cx + cw / 2, y: lobby + 21 })
    labels.push({ x: x0 + 2, y: lobby - FL.info + 2, text: `ЛАУНЖ · отдыхают ${lounging}${atCooler ? ' · у кулера ' + atCooler : ''}`, px: 3.9, color: 'rgba(255,255,255,0.9)', w: 600, align: 'left' })
    if (overflow) labels.push({ x: cx + cw, y: lobby + 47.2, text: `+${overflow} мест не хватило`, px: 3.7, color: '#ffffff', align: 'right' })
    else if (!lounging && !atCooler) labels.push({ x: cx + cw / 2, y: lobby + 47.2, text: 'кулер и диван', px: 3.7, color: 'rgba(230,230,245,0.6)' })

    // desks: the boss on top, then every cell
    const desks: Array<[Person | undefined, Geo, string]> = [[built.boss, L.boss, built.boss.name]]
    for (const h of built.heads) { const g = L.people.get(h.key); if (g) desks.push([h, g, h.name]) }
    for (const fl of L.floors) for (const cell of fl.def.cells) { const g = L.cells.get(cell.key); if (g) desks.push([cell.person, g, cell.name]) }
    for (const [p, geo, name] of desks) {
      const d = p ? now_.get(p.key)! : undefined, m = p ? memo.get(p.key)! : undefined
      const atDesk = !!d && d.place === 'desk' && !(m?.trip && m.trip.to === 'desk' && t - m.trip.t0 < WALK)
      const work = atDesk && d!.work
      const hip = geo.sy - f.shin
      drawChair(c, room, f, geo.sx, geo.sy)
      const person = () => blit(c, sprite(f, d!.pose, d!.pose === 'type' ? Math.floor(t * 5) % 2 : 0, p!.grade, p!.look, work && d!.icon !== 'think'), geo.sx, hip, false, false)
      // a desk never called stays empty: the chair and the name plate wait, nobody sits
      const seated = atDesk && !p!.waiting
      if (seated && d!.pose !== 'sleep') person()
      const scr = drawDesk(c, room, f, geo.sx, geo.sy, !!geo.wide, work, d?.icon ?? '', atDesk && d!.icon === 'coffee', geo === L.boss ? arrived : 0, t)
      if (seated && d!.pose === 'sleep') person()
      if (d?.place === 'gone' && !(m?.trip && t - m.trip.t0 < WALK)) homeSign(c, geo.sx + f.thigh + 1, geo.sy - f.shin - deskH(f))
      if (work) lights.push({ kind: 'screen', x: scr.mx, y: scr.my, bash: d!.icon === 'bash' })
      if (seated) lights.push({ kind: 'lamp', x: geo.sx + 16, y: geo.top + 30 })
      if (atDesk && d!.icon) bubbles.push({ icon: d!.icon, x: geo.sx + 2, y: geo.top + 21 })
      const cwid = geo.wide ? FL.cw * 2 : FL.cw, iy = geo.top - FL.info
      if (p && d) {
        const moving = m?.trip && t - m.trip.t0 < WALK
        const riding = !!moving && rides(m!.trip!) && tripPhase(m!.trip!.from === 'desk', (t - m!.trip!.t0) / WALK).name === 'ride'
        const icon = moving ? (riding ? 'lift' : 'walk') : d.place === 'couch' ? 'rest' : d.icon
        const text = moving ? tripText(m!.trip!, riding) : d.text
        // icons and figures glow only while at work
        if (icon) glyph(c, icon, geo.x + 2, iy + 1.5, work ? undefined : DIM)
        // one who waits to be called has nothing to report: the empty desk says it
        if (!p.waiting) labels.push({ x: geo.x + 9, y: iy + 1, text, px: 3.6, color: work ? ICON_COLOR[d.icon] ?? '#ffffff' : 'rgba(235,235,250,0.62)', align: 'left', w: work ? 600 : 400, maxw: cwid - 10 })
        if (!p.waiting) {
          // second line: the clock and its time at the left, the banknote and the money at the right;
          // the model's name only where the cell is wide — a narrow desk shows it by the outfit
          const px = geo.wide ? 3.4 : 3.1
          let lx = geo.x + 2
          // fixed slots, not measured text: the icon and its figure keep the same gap at any scale
          if (d.time && !moving) {
            glyph(c, 'clock', lx, iy + 7.8, work ? TIME_COLOR : DIM)
            labels.push({ x: lx + 7, y: iy + 7.5, text: d.time, px, color: work ? TIME_COLOR : DIM, align: 'left', w: 600 })
            lx += 25
          }
          const cost = money(p.boss ? (p.head ?? st.producer).cost : p.agent!.cost)
          if (geo.wide) {
            // a wide cell reads left to right: clock, model, banknote and the money
            labels.push({ x: lx, y: iy + 7.3, text: p.model, px, color: atDesk && d.pose !== 'sleep' ? MODEL_COLOR[p.grade] : DIM, align: 'left', w: 600, maxw: 20 })
            lx += 24
          } else {
            lx = geo.x + cwid - 24
          }
          banknote(c, lx, iy + 7.6)
          labels.push({ x: lx + 9, y: iy + 7.5, text: cost, px, color: GOLD, align: 'left', w: 600 })
        }
        // salary drips in: while working, coins fly from the screen to the banknote
        if (work) {
          const qq = ((t + hash(p.key) % 100 * 0.037) % 2.6) / 0.9
          if (qq <= 1) {
            const fx = scr.mx, fy = scr.my - 4, tx = geo.x + cwid - 7, ty = iy + 9
            c.globalAlpha = qq < 0.85 ? 1 : (1 - qq) / 0.15
            coin(c, fx + (tx - fx) * qq, fy + (ty - fy) * qq - Math.sin(Math.PI * qq) * 5)
            c.globalAlpha = 1
          }
        }
      }
      const lit = atDesk && d!.pose !== 'sleep' && !p!.waiting
      labels.push({ x: geo.x + cwid / 2, y: geo.top + 47.2, text: p ? p.name : name, px: 3.7, color: lit ? '#ffffff' : 'rgba(235,235,250,0.55)', w: lit ? 600 : 400, maxw: cwid - 3 })
    }
    // walkers go over the desks, one step closer to the viewer
    for (const w of walkers) {
      c.globalAlpha = w.fade ?? 1
      blit(c, sprite(f, 'walk', frame, w.p.grade, w.p.look, false), w.x, w.y, true, w.flip)
      c.globalAlpha = 1
    }
    // two cabins: a free one waits in the lobby, a busy one rides with its passenger
    const used = new Set(cars.map(k => k.lane))
    for (const ln of [0, 1]) if (!used.has(ln)) cars.push({ lane: ln, y: lobby + 33 })
    for (const k of cars) {
      const x = FL.pad + 1 + k.lane * 7
      box(c, x, k.y, 6, 13, '#c0c8d0', '#5a6270')
      if (k.open) R(c, x + 1, k.y + 1, 4, 11, '#fff3c0')
      if (k.rider) { R(c, x + 2, k.y + 3, 2, 2, k.rider.look.skin); R(c, x + 1, k.y + 5, 4, 6, k.rider.grade === 'senior' ? '#3a4058' : k.rider.grade === 'lead' ? '#23232f' : k.rider.look.shirt) }
    }
    // pneumatic mail: a finished job rises to the producer, a coin pops over the desk
    for (let i = flights.length - 1; i >= 0; i--) {
      const fl = flights[i]!, q = (t - fl.t0) / 1.4, cell = L.people.get(fl.key)
      if (q >= 2 || !cell) { if (q >= 2 && t - fl.t0 > 3600) flights.splice(i, 1); continue }
      if (q < 1) { const y = cell.top + 26 + (L.boss.top + 26 - cell.top - 26) * q; R(c, FL.pad + 15, y, 3, 3, '#e0b030'); R(c, FL.pad + 16, y + 1, 1, 1, '#c03040') }
      else {
        const k = q - 1, y = L.boss.top + 18 - k * 10
        c.globalAlpha = 1 - k * 0.7; coin(c, L.boss.sx + 34, y); c.globalAlpha = 1
        labels.push({ x: L.boss.sx + 40, y: y - 0.5, text: '+' + money(fl.cost), px: 3.8, color: GOLD, align: 'left', w: 600 })
      }
    }
    lighting(c, width, height, tm, lights, room)
    for (const b of bubbles) bubble(c, f.bubble, b.icon, b.x, b.y, t)   // over the light, readable at night
    return labels
  }

  function drawLabels(labels: Label[], g = octx, dpr = window.devicePixelRatio || 1) {
    g.clearRect(0, 0, g.canvas.width, g.canvas.height)
    const k = g.canvas.width / scene.width
    g.textBaseline = 'top'; g.lineJoin = 'round'
    for (const l of labels) {
      const px = Math.max(9 * dpr, l.px * k)
      g.font = l.display ? `${Math.round(px * 0.8)}px "Press Start 2P", Menlo, monospace` : `${l.w ?? 400} ${Math.round(px)}px Menlo, "JetBrains Mono", monospace`
      let text = l.text
      if (l.maxw) { const max = l.maxw * k; while (text.length > 1 && g.measureText(text).width > max) text = text.slice(0, -2) + '…' }
      const x = l.x * k, y = l.y * k
      g.textAlign = l.align ?? 'center'
      if (l.stroke !== false) { g.lineWidth = Math.max(2, px / 3.5); g.strokeStyle = 'rgba(8,8,16,0.8)'; g.strokeText(text, x, y) }
      g.fillStyle = l.color ?? '#fff'; g.fillText(text, x, y)
    }
  }

  let lastLayout = 0
  function frame() {
    try {
      const now = Date.now()
      if (now - lastLayout > 1000) { relayout(now); lastLayout = now }
      drawLabels(render(now))
    } catch (e) { onError(e) }
    requestAnimationFrame(frame)
  }
  requestAnimationFrame(frame)

  return {
    update(next: Studio) { st = next; relayout(Date.now()) },
    // another session: nothing to remember, its people start where they are, without trips for what happened meanwhile
    reset() { st = null; mem.clear(); seats.couch.fill(null); seats.cooler.fill(null); flights.length = 0 },
    setStyle(office: string, time: string) { style = { office, time }; L = null; relayout(Date.now()) },
    // this very frame, enlarged: pixels stay square, labels are drawn anew at the bigger size
    picture(zoom = 4) {
      const out = document.createElement('canvas'), text = document.createElement('canvas')
      out.width = text.width = scene.width * zoom; out.height = text.height = scene.height * zoom
      const labels = render(Date.now())
      drawLabels(labels, text.getContext('2d')!, zoom / 2)
      const g = out.getContext('2d')!
      g.imageSmoothingEnabled = false
      g.drawImage(scene, 0, 0, out.width, out.height)
      g.drawImage(text, 0, 0)
      return out
    },
  }
}
