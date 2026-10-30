// Скриншоты демо-сервера через headless Chrome и CDP. Запускается из shoot.sh: node shoot.mjs <demo> <docs> [hero|styles|codex|menubar|gif]
import { spawn, execFileSync } from 'node:child_process'
import { copyFileSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'

const [DEMO, DOCS, ...ONLY] = process.argv.slice(2)
const OUT = join(DEMO, 'out')
const BASE = 'http://localhost:8799'
const want = k => !ONLY.length || ONLY.includes(k)
const sleep = ms => new Promise(r => setTimeout(r, ms))
mkdirSync(OUT, { recursive: true })

const profile = join(DEMO, 'chrome-profile')
// порт Chrome выбирает сам и пишет его в DevToolsActivePort внутри профиля
rmSync(join(profile, 'DevToolsActivePort'), { force: true })
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
  '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
  '--force-device-scale-factor=2', '--hide-scrollbars', '--no-proxy-server', 'about:blank'], { stdio: 'ignore' })

async function connect() {
  for (let i = 0; i < 50; i++) {
    if (chrome.exitCode !== null || chrome.signalCode !== null) throw new Error('chrome завершился до старта')
    try {
      // файла может ещё не быть или он записан не целиком
      const text = readFileSync(join(profile, 'DevToolsActivePort'), 'utf8')
      const port = Number(text.split('\n')[0])
      if (text.includes('\n') && Number.isInteger(port) && port > 0) {
        const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json()
        const page = list.find(t => t.type === 'page')
        if (page) return page.webSocketDebuggerUrl
      }
    } catch { /* ещё стартует */ }
    await sleep(200)
  }
  throw new Error('chrome не поднялся')
}

let ws
let id = 0
const waiting = new Map()
const cdp = (method, params = {}) => new Promise((ok, no) => {
  const n = ++id
  waiting.set(n, m => m.error ? no(new Error(method + ': ' + m.error.message)) : ok(m.result))
  ws.send(JSON.stringify({ id: n, method, params }))
})
const js = async expr => {
  const r = await cdp('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) throw new Error(expr + ': ' + JSON.stringify(r.exceptionDetails).slice(0, 300))
  return r.result.value
}
const size = (width, height) => cdp('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 2, mobile: false })
const go = async (url, wait = 2500) => { await cdp('Page.navigate', { url }); await sleep(wait) }
const png = async (name, clip) => {
  const r = await cdp('Page.captureScreenshot', { format: 'png', ...(clip ? { clip: { ...clip, scale: 2 } } : {}) })
  writeFileSync(join(OUT, name), Buffer.from(r.data, 'base64'))
  console.log(name)
}
const click = sel => js(`document.querySelector(${JSON.stringify(sel)}).click()`)
async function setTime(k) { await js(`document.querySelector('#timeOfDay [data-k="${k}"]').click()`) }
async function setStyle(k) { await js(`document.querySelector('#officeStyle [data-k="${k}"]').click()`) }

try {
  ws = new WebSocket(await connect())
  await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = () => no(new Error('websocket chrome не открылся')) })
  ws.onmessage = e => {
    const m = JSON.parse(e.data)
    if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id) }
  }
  const sessions = await (await fetch(BASE + '/api/sessions')).json()
  const sid = sessions.find(s => s.title.startsWith('Витрина')).id
  const codex = sessions.find(s => s.provider === 'codex').id

  await cdp('Page.enable')
  await cdp('Runtime.enable')

  if (want('hero') || want('styles') || want('codex') || want('gif')) {
    await size(1440, 1000)
    await go(`${BASE}/#${sid}`)
    await js(`localStorage.setItem('office.follow','0')`)
    await setTime('day')
  }
  if (want('hero')) {
    await size(1500, 1090)
    await sleep(5000)
    await png('overview.png')
  }
  if (want('styles')) {
    await size(1440, 1340)
    const keys = await js(`[...document.querySelectorAll('#officeStyle button')].map(b => b.dataset.k)`)
    const times = await js(`[...document.querySelectorAll('#timeOfDay button')].map(b => b.dataset.k)`)
    console.log(keys, times)
    const box = await js(`(() => { const r = document.querySelector('#scene').getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height } })()`)
    for (const k of keys) {
      await setStyle(k)
      await sleep(2500)
      await png(`style-${k}.png`, box)
    }
    await setStyle(keys[0])
    await setTime('night')
    await sleep(2500)
    await png('time-night.png', box)
    await setTime('day')
  }
  if (want('codex')) {
    await size(1440, 1000)
    await click(`.sess[data-id="${codex}"]`)
    await sleep(3000)
    await png('codex.png')
  }
  if (want('gif')) {
    await size(1500, 1090)
    await click(`.sess[data-id="${sid}"]`)
    await sleep(3000)
    const box = await js(`(() => { const r = document.querySelector('#scene').getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height } })()`)
    const live = JSON.parse(readFileSync(join(DEMO, 'live.json'), 'utf8')).live
    for (const [p] of live.filter(([, , tool]) => tool === 'Write' || tool === 'Edit')) {
      const m = p.replace(/\.jsonl$/, '.meta.json')
      writeFileSync(m, JSON.stringify({ ...JSON.parse(readFileSync(m, 'utf8')), finished: true }))
    }
    await sleep(7000)
    mkdirSync(join(OUT, 'frames'), { recursive: true })
    for (let i = 0; i < 130; i++) {
      const t = Date.now()
      const r = await cdp('Page.captureScreenshot', { format: 'png', clip: { ...box, scale: 1 } })
      writeFileSync(join(OUT, 'frames', `f${String(i).padStart(3, '0')}.png`), Buffer.from(r.data, 'base64'))
      await sleep(Math.max(0, 125 - (Date.now() - t)))
    }
    console.log('frames', Date.now())
  }
  if (want('menubar')) {
    await size(380, 430)
    await go(`${BASE}/menubar.html`)
    await png('menubar-sessions.png')
    await js(`document.querySelector('[data-tab="stats"]').click()`)
    await sleep(1500)
    const h = await js(`document.documentElement.scrollHeight`)
    console.log('stats height', h)
    await size(380, Math.min(h, 2400))
    await sleep(800)
    await png('menubar-stats.png')
    await size(380, 900)
    await js(`document.querySelector('[data-tab="tokens"]').click()`)
    await sleep(1500)
    const th = await js(`document.documentElement.scrollHeight`)
    console.log('tokens height', th)
    await size(380, Math.min(th, 2400))
    await sleep(800)
    await png('menubar-tokens.png')
  }
} finally {
  ws?.close()
  // дождаться, пока Chrome реально выйдет; зависшего добиваем через SIGKILL
  if (chrome.exitCode === null && chrome.signalCode === null) {
    const exited = new Promise(r => chrome.once('exit', r))
    chrome.kill()
    const timer = setTimeout(() => chrome.kill('SIGKILL'), 3000)
    await exited
    clearTimeout(timer)
  }
}

const ffmpeg = (...args) => execFileSync('ffmpeg', ['-y', '-loglevel', 'error', ...args])
const scaled = (name, filter) => ffmpeg('-i', join(OUT, name), '-vf', filter, join(DOCS, name))
if (want('hero')) scaled('overview.png', 'scale=1800:-1:flags=lanczos')
if (want('codex')) scaled('codex.png', 'crop=2880:1480:0:0,scale=1600:-1:flags=lanczos')
if (want('styles')) for (const name of ['style-cozy.png', 'style-business.png', 'style-loft.png', 'time-night.png']) scaled(name, 'scale=1000:-1:flags=lanczos')
if (want('menubar')) for (const name of ['menubar-sessions.png', 'menubar-stats.png', 'menubar-tokens.png']) copyFileSync(join(OUT, name), join(DOCS, name))
if (want('gif')) ffmpeg('-framerate', '8', '-i', join(OUT, 'frames', 'f%03d.png'), '-vf',
  'scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle',
  '-loop', '0', join(DOCS, 'office-live.gif'))
process.exit(0)
