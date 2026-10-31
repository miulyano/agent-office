#!/usr/bin/env python3
"""Agent Office: пиксельный офис, где видно, как работают агенты Claude Code и Codex.

Читает журналы всех сессий (~/.claude/projects, ~/.codex/sessions) и отдаёт
состояние офиса выбранной сессии. В журналы ничего не пишет, хуки не нужны.

  python3 server.py [--port 8767] [--data ~/.claude/agent-office] [--watch-parent]

  /                         страница офиса
  /api/sessions             сессии за последние сутки; today — потрачено сегодня
  /api/stats?p=codex        траты по дням, моделям и папкам за всё время, почасово — за 5 недель;
                            p — один провайдер: claude или codex
  /api/state?s=<id>         офис одной сессии (руководитель, агенты, журнал, фоновые команды)
  POST /api/office?s=<id>   пересобрать офис папки сессии (новых агентов сервер сажает сам)
  POST /api/shot?name=<имя> положить скриншот (image/png) в папку скриншотов
"""
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, 'web')
CLAUDE_DIR = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')
PROJECTS = os.path.join(CLAUDE_DIR, 'projects')
SESSIONS_DIR = os.path.join(CLAUDE_DIR, 'sessions')  # живые процессы Claude Code: id журнала ↔ id в приложении
DATA = os.path.join(CLAUDE_DIR, 'agent-office')  # офисы папок; меняется флагом --data
SHOTS = os.path.expanduser('~/Pictures/Agent Office')  # скриншоты офиса; меняется флагом --shots
SHOT_LIMIT = 64 * 1024 * 1024
HOSTS = {'localhost', '127.0.0.1'}  # имена, по которым приходит своя страница; None — любые, см. trusted_hosts
CODEX_DIR = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
CODEX_SESSIONS = os.path.join(CODEX_DIR, 'sessions')
CODEX_INDEX = os.path.join(CODEX_DIR, 'session_index.jsonl')  # названия тредов
CODEX_ACCENT = '#4fb3c8'  # вывеска офиса Codex
RECENT_S = 24 * 3600     # сессии в списке
LIVE_S = 10 * 60         # тишина дольше — сессия считается остывшей
AGENT_IDLE_S = 10 * 60   # агент без finished и без записей дольше — ушёл
BG_DONE_S = 2 * 60       # столько завершённая фоновая команда стоит в серверной
BG_FAILED_S = 10 * 60    # упавшая дольше: её надо успеть заметить
BG_LOST_S = 3 * 3600     # команда без итога дольше — потеряна: журнал не пишет, что сессию закрыли
CODEX_WORK = ('reasoning', 'custom_tool_call', 'custom_tool_call_output', 'function_call', 'function_call_output')
INTERRUPTED = '[Request interrupted'
NOT_WORK = (INTERRUPTED, '<command-name>', '<command-message>', '<local-command-')  # за такими записями работы нет: для часов это не запрос
CALLED_AGAIN = 'The coordinator sent'  # так в журнале агента выглядит повторный вызов
LOG_MAX = 80
# $ за 1M токенов: вход, выход, чтение кэша. Цены меняются — правьте здесь.
PRICES = {
    'claude-fable-5-1': (10.0, 50.0, 0.25),
    'claude-opus-5-5': (4.0, 20.0, 0.20),
    'claude-sonnet-5-5': (2.0, 10.0, 0.20),
    'claude-haiku-5-5': (0.10, 0.50, 0.01),
    'gpt-6-astra': (10.0, 50.0, 1.0),
    'gpt-6.1-sol': (2.0, 10.0, 0.10),
    'gpt-6.1-terra': (1.0, 5.0, 0.05),
    'gpt-6-sol': (2.0, 10.0, 0.20),
    'gpt-6-luna': (0.10, 0.50, 0.01),
    'gpt-5.6-sol': (4.0, 20.0, 0.40),
    'gpt-5.6-terra': (2.0, 12.0, 0.20),
    'gpt-5.6-luna': (0.20, 1.20, 0.02),
    'gpt-5.5': (5.0, 30.0, 0.50),
    'gpt-5.4': (2.5, 15.0, 0.25),
    'gpt-5.3-codex': (1.75, 14.0, 0.175),
    'gpt-5.2': (1.75, 14.0, 0.175),
    'gpt-5.1': (1.25, 10.0, 0.125),
    'gpt-5': (1.25, 10.0, 0.125),
}
LOCK = threading.Lock()  # журналы дочитываются по одному
LINKED_RE = re.compile(r'(\S+) = (local_[0-9a-f-]+) \(')
BACKGROUND_RE = re.compile(r'^Background \(from the session that started this one\)', re.M)
TASK_NOTE_RE = re.compile(r'<task-notification>(.*?)</task-notification>', re.S)
TASK_ID_RE = re.compile(r'<task-id>([^<]+)</task-id>')
TASK_STATUS_RE = re.compile(r'<status>([^<]+)</status>')
MARKS = '①②③④⑤⑥⑦⑧⑨'


def ms(ts):
    """ISO-время журнала → миллисекунды."""
    if not ts:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1000)
    except ValueError:
        return 0


TOOL_KINDS = {'Read': 'read', 'Write': 'write', 'Edit': 'edit', 'MultiEdit': 'edit', 'NotebookEdit': 'edit',
              'Grep': 'grep', 'Glob': 'grep', 'Bash': 'bash', 'WebSearch': 'web', 'WebFetch': 'web',
              'Agent': 'agent', 'Task': 'agent', 'Skill': 'skill'}


def tool_kind(tool):
    """Вид инструмента для значка в офисе: читает, правит, ищет, терминал…"""
    return TOOL_KINDS.get(tool, 'other')


def base(path):
    return os.path.basename(str(path or ''))


def describe(tool, inp):
    """Вызов инструмента — парой слов, как его видно через офис."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == 'Read':
        return 'читает ' + base(inp.get('file_path'))
    if tool == 'Write':
        return 'пишет ' + base(inp.get('file_path'))
    if tool in ('Edit', 'NotebookEdit', 'MultiEdit'):
        return 'правит ' + base(inp.get('file_path'))
    if tool in ('Grep', 'Glob'):
        return 'ищет ' + str(inp.get('pattern', ''))
    if tool in ('WebSearch', 'WebFetch'):
        return 'гуглит'
    if tool == 'Skill':
        return 'скил ' + str(inp.get('skill', ''))
    if tool in ('Agent', 'Task'):
        return 'зовёт: ' + str(inp.get('subagent_type') or 'general-purpose')
    if tool == 'Bash':
        desc = str(inp.get('description') or '').strip()
        if desc:
            return desc
        cmd = re.sub(r'^(cd \S+\s*(&&|;)\s*)+', '', str(inp.get('command', '')).strip())
        words = cmd.split()
        if words and words[0] in ('until', 'sleep'):
            return 'ждёт'
        if words and words[0] in ('for', 'while') and words[0] != cmd:
            return 'перебирает в цикле'
        if words and words[0] in ('python3', 'python', 'node') and (len(words) == 1 or words[1].startswith('-')):
            return 'считает скриптом'
        if words and words[0] in ('python3', 'python', 'node', 'bash', 'sh') and len(words) > 1:
            return '$ ' + ' '.join([base(words[1])] + words[2:3])
        return '$ ' + ' '.join(words[:2])
    if tool == 'ToolSearch':
        return 'ищет инструмент'
    if tool == 'SendMessage':
        return 'пишет агенту'
    if tool == 'SubagentHandback':
        return 'докладывает руководителю'
    if tool.startswith('mcp__ccd_turn'):
        return 'отмечает план'
    if tool.startswith('mcp__ccd_'):
        return 'двигает окна'
    if tool.startswith('mcp__Claude_Browser'):
        return 'смотрит в браузер'
    if tool.startswith('mcp__claude-in-chrome'):
        return 'работает в Chrome'
    if 'notion' in tool:
        return 'листает Notion'
    if tool.startswith('mcp__life-transcriber'):
        return 'транскрибирует'
    if tool.startswith('mcp__epidemic'):
        return 'ищет музыку'
    if tool.startswith('mcp__'):
        return ' '.join(tool.split('__')[1:])
    return tool


def cache_writes(usage):
    """Записи в кэш: (на 5 минут, на час)."""
    cc = usage.get('cache_creation') or {}
    if not cc:
        return usage.get('cache_creation_input_tokens', 0), 0
    return cc.get('ephemeral_5m_input_tokens', 0), cc.get('ephemeral_1h_input_tokens', 0)


def cost_parts(model, usage):
    """Стоимость ответа по статьям: вход, выход, чтение кэша, запись в кэш."""
    if model not in PRICES:
        return [0.0, 0.0, 0.0, 0.0]
    p_in, p_out, p_read = PRICES[model]
    w5m, w1h = cache_writes(usage)
    k = (2 if usage.get('speed') == 'fast' else 1) / 1e6
    return [usage.get('input_tokens', 0) * p_in * k, usage.get('output_tokens', 0) * p_out * k,
            usage.get('cache_read_input_tokens', 0) * p_read * k, (w5m * 1.25 + w1h * 2) * p_in * k]


def provider_of(model):
    """Чей ответ: Claude Code или Codex — по имени модели."""
    return 'codex' if str(model).startswith(('gpt-', 'codex-')) else 'claude'


def message_cost(model, usage):
    """Стоимость одного ответа модели, формула reel.py cost."""
    return sum(cost_parts(model, usage))


def token_parts(usage):
    """Токены ответа по статьям, в том же порядке, что cost_parts."""
    return [usage.get('input_tokens', 0), usage.get('output_tokens', 0),
            usage.get('cache_read_input_tokens', 0), sum(cache_writes(usage))]


def tokens_of(usage):
    return sum(token_parts(usage))


def day_of(at_ms):
    """Локальная календарная дата записи журнала."""
    return time.strftime('%Y-%m-%d', time.localtime(at_ms / 1000))


def codex_usage(meter, d):
    """Запись журнала Codex → токены ответа в виде Anthropic: вход без кэша, кэш отдельно.
    С сентября 2026 ответы пишутся по одному (token_usage_record); в старых журналах есть
    только нарастающий итог token_count. Состояние между записями лежит в meter."""
    kind, p = d.get('type'), d.get('payload') or {}
    if kind == 'session_meta':
        if codex_origin(p)[1] == 'guardian':
            meter.setdefault('model', 'codex-auto-review')  # после сжатия тред начинается без turn_context
        return None
    if kind == 'turn_context':
        meter['model'] = p.get('model') or meter.get('model')
        return None
    if kind == 'event_msg' and p.get('type') == 'thread_settings_applied':
        settings = p.get('thread_settings') or {}
        meter['model'] = settings.get('model') or meter.get('model')
        meter['tier'] = settings.get('service_tier') or meter.get('tier')
        return None
    if kind == 'token_usage_record':
        meter['records'] = True
        u = p.get('usage') or {}
    elif kind == 'event_msg' and p.get('type') == 'token_count':
        total = (p.get('info') or {}).get('total_token_usage')
        if not total:
            return None
        prev, meter['prev'] = meter.get('prev') or {}, total
        # итог падает после отката треда: это не новая трата
        if meter.get('records') or total.get('total_tokens', 0) < prev.get('total_tokens', 0):
            return None
        u = {k: v - prev.get(k, 0) for k, v in total.items()}
    else:
        return None
    cached, written = u.get('cached_input_tokens', 0), u.get('cache_write_input_tokens', 0)
    usage = {'input_tokens': max(0, u.get('input_tokens', 0) - cached - written), 'output_tokens': u.get('output_tokens', 0),
             'cache_read_input_tokens': cached, 'cache_creation_input_tokens': written}
    if not any(usage.values()):
        return None
    if meter.get('tier') == 'fast':
        usage['speed'] = 'fast'
    return usage


def codex_model(meter):
    return meter.get('model') or 'gpt-?'


def codex_origin(meta):
    """Тред внутри другого: (id родителя, роль). Ревью действий — контролёр, остальные — фрилансеры."""
    source = meta.get('source')
    sub = source.get('subagent') if isinstance(source, dict) else None
    if not sub:
        return '', ''
    spawn = sub.get('thread_spawn') if isinstance(sub, dict) else None
    parent = meta.get('parent_thread_id') or (spawn or {}).get('parent_thread_id') or ''
    return parent, 'general-purpose' if spawn else 'guardian'


def today_cost(t):
    return t.daily.get(day_of(time.time() * 1000), 0.0)


def parent_alive(ppid):
    return os.getppid() == ppid


def clip(text, n=60):
    text = ' '.join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + '…'


def home_of(cwd):
    """Папка проекта: рабочая копия worktree считается своим репозиторием."""
    if not cwd:
        return ''
    return re.split(r'/\.claude/worktrees(?:/|$)', cwd)[0]


def folder_name(path):
    """Имя папки для людей: у ChatGPT-проектов вместо него идентификатор."""
    name = os.path.basename(str(path or '').rstrip('/'))
    return 'ChatGPT-проект' if re.match(r'g-p-[0-9a-f]+$', name) else name


def codex_item(it):
    """Законченный шаг Codex → инструмент Claude Code, чтобы описать его теми же словами."""
    kind = it.get('type')
    if kind == 'CommandExecution':
        parsed = (it.get('parsed_cmd') or [{}])[0]
        if parsed.get('type') == 'read':
            return 'Read', {'file_path': parsed.get('path') or parsed.get('name')}
        if parsed.get('type') == 'search':
            return 'Grep', {'pattern': parsed.get('query') or parsed.get('cmd')}
        if parsed.get('type') == 'list_files':
            return 'Glob', {'pattern': parsed.get('path') or '.'}
        command = it.get('command') or ['']
        return 'Bash', {'command': command[-1] if isinstance(command, list) else command}
    if kind == 'FileChange':
        changes = it.get('changes') or {}
        path = next(iter(changes), '')
        added = (changes.get(path) or {}).get('type') == 'add'
        return ('Write' if added else 'Edit'), {'file_path': path}
    if kind == 'McpToolCall':
        return 'mcp__%s__%s' % (it.get('server'), it.get('tool')), {}
    if kind == 'WebSearch':
        return 'WebSearch', {}
    if kind == 'ImageView':
        return 'Read', {'file_path': it.get('path')}
    return None


CODEX_CMD = re.compile(r'\b(?:cmd|command)"?\s*:\s*(["`\'])((?:\\.|(?!\1).)*)\1', re.S)
CODEX_TOOL = re.compile(r'tools\.(\w+)\s*\(')
CODEX_CALLS = {'apply_patch': ('Edit', {}), 'view_image': ('Read', {}), 'web__run': ('WebSearch', {})}


def has_tool_result(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content)


def first_text(content):
    return (content if isinstance(content, str) else next((b.get('text', '') for b in content or [] if isinstance(b, dict) and b.get('type') == 'text'), '')).lstrip()


def shell_shown(shell, now_ms):
    if not shell['end']:
        return now_ms - shell['start'] < BG_LOST_S * 1000
    keep = BG_FAILED_S if shell['status'] == 'failed' else BG_DONE_S
    return now_ms - shell['end'] < keep * 1000


def work_clock(spans, now_ms):
    """Сколько шла хоть какая-то работа: параллельные отрезки считаются один раз.
    Идущий отрезок отдаём его началом: иначе ответ менялся бы каждую секунду, а досчитывает страница."""
    total = start = reached = 0
    for a, b in sorted(spans):
        if a > reached:
            total, start = total + reached - start, a
        reached = max(reached, b)
    if reached >= now_ms:
        return {'worked': int(total), 'workingSince': int(start)}
    return {'worked': int(total + reached - start), 'workingSince': 0}


def own_clock(agent, now_ms):
    """Часы агента. Сдал работу: они стоят, хотя его фоновая команда ещё идёт в общее время сессии."""
    end = agent['endedAt']
    spans = [[a, min(b, end)] for a, b in agent['spans'] if a < end] if end else agent['spans']
    return work_clock(spans, now_ms)


def clock_of(tails, agents, now_ms):
    return work_clock([s for t in tails for s in t.worked(now_ms)] + [s for a in agents for s in a['spans']], now_ms)


class Tail:
    """Журнал, дочитываемый с места остановки: вызовы, последнее действие, хвост."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.calls = 0
        self.first = 0
        self.last = 0          # время последней записи
        self.turn = 0          # начало текущего хода: последний запрос
        self.requests = 0      # сколько запросов получил руководитель
        self.spans = []        # ходы: [начало, последняя запись]
        self.open = False      # последний ход ещё не закончен
        self.called = 0        # служебная запись: от неё считается работа, если за ней кто-то возьмётся
        self.activity = ''
        self.activity_at = 0
        self.tool = ''         # вид последнего инструмента, см. tool_kind
        self.model = ''        # модель последнего ответа
        self.busy = False      # последний ход не закончен
        self.title = ''
        self.prompt = ''
        self.cwd = ''
        self.home = ''         # папка, где сессию открыли
        self.branch = ''
        self.roster = {}       # агенты, доступные сессии: тип → описание
        self.linked = {}       # сессии, которые эта породила через приложение: local_id → имя
        self.child = False     # сессию породила другая: в первом запросе её вводная
        self.events = []       # [(at, text)]
        self.cost = 0.0
        self.daily = {}        # локальная дата → $, последние два дня
        self.paid = set()      # ответ модели пишется в журнал кусками — платим за id один раз
        self.calling_shells = {}  # вызовы Bash без результата: id → (at, что делает)
        self.calling_tools = {}   # все вызовы без результата: id → at
        self.shell_seen = {}      # до какого момента команду без итога видели идущей: id задачи → at
        self.shells = {}       # фоновые команды: id задачи → запись для серверной

    def feed(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return self
        if size < self.offset:
            self.__init__(self.path)
        if size == self.offset:
            return self
        try:
            for raw, self.offset in journal_lines(self.path, self.offset):
                try:
                    self.take(json.loads(raw))
                except Exception:  # чужая запись не должна стоить всего списка сессий
                    continue
        except OSError:
            pass  # журнал удалили между проверкой и чтением
        return self

    def work(self, at, start=False):
        """Запрос начинает новый ход, записи работы тянут конец текущего."""
        if not at:
            return
        if start or not self.open:
            called = self.called if at - self.called < LIVE_S * 1000 else at
            self.spans.append([at if start else called, at])
            self.open, self.called = True, 0
        else:
            self.spans[-1][1] = max(self.spans[-1][1], at)

    def running(self, now_ms):
        """Незаконченный ход идёт: журнал пишется или ждёт результата вызова. Долгий вызов журнал не трогает."""
        fresh = now_ms - self.last < LIVE_S * 1000
        return self.open and (fresh or any(now_ms - at < BG_LOST_S * 1000 for at in self.calling_tools.values()))

    def worked(self, now_ms, alive=True):
        """Отрезки работы: ходы и фоновые команды. alive=False: идти уже нечему, агент сдал работу.
        Что сейчас идёт, дописываем в память: показанное время назад не откатывается."""
        if alive and self.spans and self.running(now_ms):
            self.spans[-1][1] = max(self.spans[-1][1], now_ms)
        spans = [list(s) for s in self.spans]
        for s in self.shells.values():
            if not s['end'] and shell_shown(s, now_ms):
                self.shell_seen[s['id']] = now_ms
            # команда без итога потеряна: журнал не пишет, когда она встала
            end = s['end'] or self.shell_seen.get(s['id'], self.last)
            if s['start']:
                spans.append([s['start'], max(s['start'], end)])
        return spans

    def close(self, now_ms):
        """Процесса сессии не стало: всё идущее остановилось."""
        self.open = False
        self.calling_tools.clear()
        for s in self.shells.values():
            if not s['end'] and shell_shown(s, now_ms):
                s.update(status='killed', end=now_ms)

    def listing(self, a):
        """Список агентов приходит в журнал дельтами: добавлены, убраны."""
        if a.get('type') != 'agent_listing_delta':
            return
        for t in a.get('removedTypes') or []:
            self.roster.pop(t, None)
        descs = {}
        for line in a.get('addedLines') or []:
            m = re.match(r'- ([^:]+(?::[^:\s]+)?): (.*)', line)
            if m:
                descs[m.group(1)] = m.group(2)
        for t in a.get('addedTypes') or []:
            self.roster[t] = descs.get(t, '')

    def take(self, d):
        kind = d.get('type')
        if kind == 'custom-title':
            self.title = d.get('customTitle') or self.title
            return
        if kind == 'last-prompt':
            self.prompt = d.get('lastPrompt') or self.prompt
            return
        if kind == 'attachment':
            self.listing(d.get('attachment') or {})
            self.notified((d.get('attachment') or {}).get('prompt'), ms(d.get('timestamp')))
            return
        if kind == 'queue-operation':
            if d.get('operation') == 'enqueue':
                self.notified(d.get('content'), ms(d.get('timestamp')))
            return
        if kind not in ('user', 'assistant'):
            return
        at = ms(d.get('timestamp'))
        if at:
            self.first = self.first or at
            self.last = max(self.last, at)
        self.cwd = d.get('cwd') or self.cwd
        self.home = self.home or home_of(self.cwd)
        self.branch = d.get('gitBranch') or self.branch
        msg = d.get('message') or {}
        content = msg.get('content') if isinstance(msg, dict) else None
        if kind == 'user':
            # новый запрос или результат инструмента — ход продолжается
            self.busy = True
            self.note_links(content, at)
            self.shell_answered(d, content, at)
            if isinstance(content, str) and not d.get('isMeta'):
                self.activity, self.activity_at = 'думает', at
            result = has_tool_result(content)
            if not d.get('isMeta') and not d.get('isCompactSummary') and not result:
                self.turn = at or self.turn
            text = first_text(content)
            own = not d.get('isMeta') and not text.startswith(NOT_WORK)
            if not d.get('isCompactSummary') and not result and (own or text.startswith(CALLED_AGAIN)):
                self.requests += 1
            if text.startswith(INTERRUPTED):
                self.calling_tools.clear()  # результата прерванного вызова можно не дождаться
            if d.get('isMeta') or text.startswith(NOT_WORK):
                # в журнале агента нет end_turn, а повторный вызов приходит такой записью: пауза до неё не работа
                if not self.calling_tools:
                    self.open, self.called = False, at
            elif not d.get('isCompactSummary'):
                self.work(at, not result)
            return
        mid = msg.get('id')
        self.model = msg.get('model') or self.model
        if msg.get('usage') and mid not in self.paid:
            self.paid.add(mid)
            self.pay(msg.get('model'), msg['usage'], at)
        uses = [b for b in content or [] if isinstance(b, dict) and b.get('type') == 'tool_use']
        for b in uses:
            self.calls += 1
            what = describe(b.get('name', ''), b.get('input'))
            self.activity, self.activity_at = what, at
            self.tool = tool_kind(b.get('name', ''))
            self.events.append((at, what))
            if b.get('id'):
                self.calling_tools[b['id']] = at
            if b.get('name') == 'Bash' and b.get('id'):
                self.calling_shells[b['id']] = (at, what)
        if len(self.events) > LOG_MAX:
            self.events = self.events[-LOG_MAX:]
        self.busy = msg.get('stop_reason') != 'end_turn' or bool(uses)
        self.work(at)
        self.open = self.busy and bool(self.spans)

    def shell_answered(self, d, content, at):
        """Результат вызова: команда либо закончилась, либо ушла в фон. Считаем от самого вызова."""
        result = d.get('toolUseResult')
        task = result.get('backgroundTaskId') if isinstance(result, dict) else None
        # TaskStop обычно уведомления не присылает: остановку видно по самому ответу
        stopped = result.get('task_id') if isinstance(result, dict) else None
        shell = self.shells.get(stopped) if isinstance(stopped, str) else None
        if shell and not shell['end'] and at:
            shell.update(status='killed', end=at)
        for b in content if isinstance(content, list) else []:
            call = self.calling_shells.pop(b.get('tool_use_id'), None) if isinstance(b, dict) else None
            if isinstance(b, dict):
                self.calling_tools.pop(b.get('tool_use_id'), None)
            if call and task:
                self.shells[task] = {'id': task, 'name': call[1], 'status': 'running', 'start': call[0] or at, 'end': 0}

    def notified(self, text, at):
        """Итог приходит уведомлением, одним и тем же в нескольких записях: время берём из первой."""
        # каждое уведомление разбираем отдельно: без статуса оно не должно занять чужой
        for block in TASK_NOTE_RE.findall(text) if isinstance(text, str) and at else ():
            task, status = TASK_ID_RE.search(block), TASK_STATUS_RE.search(block)
            shell = self.shells.get(task.group(1)) if task and status else None
            if shell and not shell['end']:
                shell.update(status=status.group(1), end=at)

    def running_shells(self, now_ms):
        return sum(1 for s in self.shells.values() if shell_shown(s, now_ms) and not s['end'])

    def note_links(self, content, at=0):
        """Приложение дописывает в запрос, кого эта сессия породила, а породившей — вводную."""
        # результаты инструментов не считаются: там эти фразы могут быть процитированы
        texts = [content] if isinstance(content, str) else [b.get('text', '') for b in content or [] if isinstance(b, dict) and b.get('type') == 'text']
        for text in texts:
            if BACKGROUND_RE.search(text):
                self.child = True
            if 'Linked sessions now' in text:
                self.linked = {m.group(2): m.group(1) for m in LINKED_RE.finditer(text)}
            self.notified(text, at)

    def pay(self, model, usage, at):
        usd = message_cost(model, usage)
        self.cost += usd
        day = day_of(at)
        self.daily[day] = self.daily.get(day, 0.0) + usd
        for old in sorted(self.daily)[:-2]:
            del self.daily[old]


def task_work(kind, p):
    """Запись Codex о самой работе. Остальные бывают и вне задачи: при возобновлении и перед новой."""
    if kind == 'response_item':
        return p.get('role') == 'assistant' or p.get('type') in CODEX_WORK
    if kind == 'event_msg':
        return p.get('type') in ('item_completed', 'task_complete', 'turn_aborted')
    return kind == 'token_usage_record'


class CodexTail(Tail):
    """Журнал сессии Codex: поля те же, что у Tail, записи другие."""

    def __init__(self, path):
        super().__init__(path)
        self.thread = ''
        self.parent = ''       # тред, который запустил этот
        self.role = ''         # guardian или general-purpose; у главной сессии пусто
        self.nickname = ''
        self.meter = {}

    def take(self, d):
        kind, p = d.get('type'), d.get('payload') or {}
        at = ms(d.get('timestamp'))
        if at:
            self.first = self.first or at
            self.last = max(self.last, at)
            # задача могла оборваться без task_complete: тянем её только записями самой работы
            if self.open and task_work(kind, p):
                self.work(at)
        usage = codex_usage(self.meter, d)
        if kind == 'session_meta':
            if not self.thread:
                self.thread = p.get('id') or ''
                self.parent, self.role = codex_origin(p)
                self.nickname = p.get('agent_nickname') or ''
                self.cwd = p.get('cwd') or ''
                self.home = home_of(self.cwd)
            return
        if usage and at:
            self.pay(codex_model(self.meter), usage, at)
        if kind == 'turn_context':
            self.model = self.meter.get('model') or self.model
            self.cwd = p.get('cwd') or self.cwd
        elif kind == 'response_item' and p.get('type') in ('custom_tool_call', 'function_call'):
            self.calling(p, at)
        elif kind == 'response_item' and p.get('type') in ('custom_tool_call_output', 'function_call_output'):
            self.calling_tools.pop(p.get('call_id'), None)
        elif kind == 'event_msg':
            event = p.get('type')
            if event == 'task_started':
                self.busy = True
                self.turn = at or self.turn
                self.requests += 1
                self.work(at, True)
                self.activity, self.activity_at = 'думает', at
            elif event in ('task_complete', 'turn_aborted'):
                self.busy = self.open = False
                self.calling_tools.clear()  # у оборванного вызова результата не будет
            elif event == 'item_completed':
                self.finished(p.get('item') or {}, at)

    def calling(self, p, at):
        """Вызов только начался: в журнале он виден сразу, а итог появится, когда закончится."""
        if p.get('call_id'):
            self.calling_tools[p['call_id']] = at
        text = str(p.get('input') or p.get('arguments') or '')
        name = p.get('name') or ''
        inner = CODEX_TOOL.search(text)
        name = inner.group(1) if inner else name
        tool, inp = CODEX_CALLS.get(name, ('', {}))
        cmd = CODEX_CMD.search(text)
        if name.startswith('mcp__'):
            tool = name
        elif name in ('exec_command', 'shell', 'shell_command') and cmd:
            tool, inp = 'Bash', {'command': cmd.group(2)}
        if tool:
            self.activity, self.activity_at, self.tool = describe(tool, inp), at, tool_kind(tool)

    def finished(self, it, at):
        if it.get('type') == 'UserMessage':
            text = next((c.get('text') for c in it.get('content') or [] if isinstance(c, dict) and c.get('text')), '')
            self.prompt = self.prompt or text
            return
        step = codex_item(it)
        if not step:
            return
        self.calls += 1
        what = describe(*step)
        self.activity, self.activity_at, self.tool = what, at, tool_kind(step[0])
        self.events.append((at, what))
        del self.events[:-LOG_MAX]


# ── Офисы: раскладка агентов папки по столам ───────────────────────────────
# универсальные агенты сидят во фрилансе, своих столов у них нет
BUILTIN = {'claude', 'general-purpose', 'Explore', 'Plan', 'claude-code-guide', 'statusline-setup'}
PROPS = ['crown', 'headphones', 'camera', 'scissors', 'beret', 'beretRed', 'books', 'chart', 'mic',
         'binoculars', 'script', 'box', 'download', 'magnifier', 'layers', 'frame', 'glasses',
         'tablet', 'clipboard', 'paper', 'cap']
SHIRTS = ['#2f4f8f', '#c0583a', '#5a7a2e', '#8f2f3a', '#4b5a8a', '#3a7a4a', '#7a3fb0', '#1f6f8f',
          '#b08a2e', '#2f7a5a', '#c04a6a', '#8a6a3a', '#a0602a', '#2f6f6a', '#6b2d5c', '#5a5f6a']
HAIRS = ['#2b1d14', '#1a1a1a', '#6b4a2b', '#c9a227', '#9a9a9a', '#3a2416', '#8a5a2b', '#e05a8a']
GEN_TIMEOUT = 120  # с: большой офис собирается за 15–30 с; дольше — claude чего-то ждёт
GEN_QUIET = 45     # с: после старта модель молчит дольше — до API не дошли
GEN_RETRIES = 2    # столько повторов запроса подряд — связи нет, дальше не ждём
GEN_SLOT = threading.Lock()  # генерации идут по одной
GEN_BUDGET = '0.5'  # $ на одну генерацию: обычно хватает 4–8 центов
NO_NETWORK = 'нет связи с API: проверьте интернет и прокси'
STAFF_EVERY = 30   # с между проверками штата папок
OFFICE_V = 2       # 2: у отделов id, у офиса язык, у столов short и locked
LANG = 'ru'        # язык новых офисов; подписи живут в name и short, раскладка от языка не зависит
SIGNS = {'ru': {'boss': 'Главный', 'other': 'ПРОЧИЕ', 'support': 'ПОДДЕРЖКА', 'language': 'по-русски, без латиницы (вместо QA — «Контролёр»)'},
         'en': {'boss': 'Lead', 'other': 'OTHER', 'support': 'SUPPORT', 'language': 'in English'}}
AUTO_TRIED = {}    # папка → новички, для которых генерация уже запускалась
JOBS_LOCK = threading.Lock()  # проверка «уже идёт» и запись новой генерации — одним шагом
OFFICE_LOCK = threading.Lock()  # файл офиса пишут генерация и проверка штата
JOBS = {}  # папка → последняя генерация: status running|done|failed|cancelled, started, ended, error, proc


def hash_of(text):
    h = 0
    for c in text:
        h = (h * 31 + ord(c)) & 0x7fffffff
    return h


def office_path(folder):
    return os.path.join(DATA, 'offices', re.sub(r'[^A-Za-z0-9]+', '-', folder).strip('-') + '.json')


def dress(desk):
    """Цвета, которых нет в описании стола, — по типу агента, стабильно."""
    h = hash_of(desk['type'])
    desk.setdefault('shirt', SHIRTS[h % len(SHIRTS)])
    desk.setdefault('hair', HAIRS[(h // 7) % len(HAIRS)])
    desk.setdefault('skin', (h // 13) % 5)
    if desk.get('prop') not in PROPS:
        desk['prop'] = PROPS[(h // 3) % len(PROPS)]
    return desk


def humanize(t):
    key = t.split(':')[-1].replace('-', ' ').replace('_', ' ')
    return clip(key[:1].upper() + key[1:], 13)


# Должности по-русски без модели: сначала словосочетание целиком, потом главное слово (последнее).
PHRASES = {
    'art-director': 'Арт-директор', 'motion-designer': 'Аниматор', 'sound-designer': 'Звукорежиссёр',
    'cover-maker': 'Обложечник', 'subs-editor': 'Субтитровщик', 'qa-inspector': 'Контролёр', 'qa': 'Контролёр',
    'style-critic': 'Стилист', 'selection-critic': 'Отборщик', 'code-reviewer': 'Ревьюер', 'statusline-setup': 'Наладчик',
    'guardian': 'Контролёр', 'general-purpose': 'Универсал', 'claude': 'Помощник', 'claude-code-guide': 'Проводник',
}
# Предмет на столе встроенного агента, когда офис собирают без модели
GUEST_PROPS = {'Explore': 'binoculars', 'Plan': 'chart', 'general-purpose': 'layers', 'claude': 'tablet',
               'claude-code-guide': 'books', 'statusline-setup': 'headphones'}
WORDS = {
    'reviewer': 'Ревьюер', 'review': 'Ревьюер', 'builder': 'Сборщик', 'investigator': 'Сыщик', 'rescue': 'Спасатель',
    'analyst': 'Аналитик', 'director': 'Директор', 'camera': 'Оператор', 'copywriter': 'Копирайтер', 'maker': 'Мастер',
    'editor': 'Редактор', 'interviewer': 'Интервьюер', 'inspector': 'Инспектор', 'librarian': 'Библиотекарь',
    'loader': 'Загрузчик', 'designer': 'Дизайнер', 'packager': 'Упаковщик', 'scout': 'Скаут', 'scriptwriter': 'Сценарист',
    'critic': 'Критик', 'writer': 'Автор', 'planner': 'Планировщик', 'plan': 'Архитектор', 'architect': 'Архитектор',
    'tester': 'Тестировщик', 'researcher': 'Исследователь', 'explorer': 'Разведчик', 'explore': 'Разведчик',
    'coder': 'Программист', 'developer': 'Разработчик', 'auditor': 'Аудитор', 'fixer': 'Наладчик', 'helper': 'Помощник',
    'assistant': 'Помощник', 'manager': 'Менеджер', 'translator': 'Переводчик', 'summarizer': 'Конспектор',
    'debugger': 'Отладчик', 'documenter': 'Документатор', 'guide': 'Гид', 'deployer': 'Выкладчик', 'optimizer': 'Оптимизатор',
    'refactorer': 'Рефакторщик', 'migrator': 'Мигратор', 'monitor': 'Наблюдатель', 'operator': 'Оператор',
}
# Отделы по ходу работы: найти → придумать → сделать → проверить → выручить
KINDS = [
    ('РАЗВЕДКА', ['investigat', 'scout', 'explor', 'research', 'search', 'analyst', 'librarian', 'finder', 'locator', 'monitor']),
    ('ПЛАН', ['plan', 'architect', 'script', 'copywrit', 'writer', 'interview', 'strateg', 'product']),
    ('ПРОИЗВОДСТВО', ['build', 'editor', 'maker', 'design', 'director', 'camera', 'motion', 'sound', 'loader', 'packag',
                      'render', 'cover', 'subs', 'coder', 'develop', 'refactor', 'migrat', 'translat', 'document', 'operator']),
    ('КАЧЕСТВО', ['review', 'critic', 'inspector', 'qa', 'test', 'audit', 'verif', 'lint', 'check', 'guardian']),
    ('ПОДДЕРЖКА', ['rescue', 'debug', 'fix', 'deploy', 'setup', 'support', 'ops', 'help', 'guide']),
]
FOREIGN = {'ru': re.compile('[A-Za-z]'), 'en': re.compile('[А-Яа-яЁё]')}


def ru_name(t):
    key = t.split(':')[-1].lower()
    if key in PHRASES:
        return PHRASES[key]
    head = re.split(r'[-_ ]+', key)[-1]
    return WORDS.get(head) or humanize(t)


def kind_of(t, desc=''):
    key = t.split(':')[-1].lower()
    for text in (key, desc.lower()):
        for zone, words in KINDS:
            if any(w in text for w in words):
                return zone
    return 'ПРОЧИЕ'


def in_lang(text, lang):
    """Подпись, если она на языке офиса; иначе пусто."""
    text = ' '.join(str(text or '').split())
    return text if text and not FOREIGN[lang].search(text) else ''


def local_name(t, lang):
    return ru_name(t) if lang == 'ru' else humanize(t)


def zone_id(name):
    return re.sub(r'\W+', '-', str(name).strip().lower()).strip('-')


def guests_of(types, lang, known=None):
    """Встроенные агенты сессии: имя из сохранённого офиса, иначе без модели."""
    known = known or {}
    return {t: known.get(t) or {'name': local_name(t, lang), 'prop': GUEST_PROPS.get(t, 'cap')} for t in types if t in BUILTIN}


def auto_office(folder, roster, shirt='#3b3f8f'):
    """Офис без модели: отделы по ходу работы, должности по-русски из названий агентов."""
    order = [k for k, _ in KINDS] + ['ПРОЧИЕ']
    zones = {}
    for t, desc in roster.items():
        if t in BUILTIN:
            continue
        zones.setdefault(kind_of(t, desc or ''), []).append(dress({'type': t, 'name': ru_name(t)}))
    boss = dress({'type': 'main', 'name': 'Главный', 'prop': 'crown', 'shirt': shirt})
    rows = [{'id': 'главный', 'name': 'ГЛАВНЫЙ', 'desks': [boss]}] if not zones else []
    for i, name in enumerate(n for n in order if n in zones):
        rows.append({'id': zone_id(name), 'name': name, 'desks': ([boss] if i == 0 else []) + zones[name]})
    return {'v': OFFICE_V, 'lang': LANG, 'folder': folder, 'title': 'ОФИС · ' + os.path.basename(folder).upper(), 'zones': rows, 'guests': guests_of(roster, LANG)}


def upgrade(office):
    """Файл старого формата. На диск новый вид ляжет при следующей записи."""
    office['v'] = OFFICE_V
    office.setdefault('lang', LANG)
    for z in office.get('zones') or []:
        z.setdefault('id', zone_id(z.get('name', '')))
    return office


def load_office(folder):
    try:
        with open(office_path(folder)) as f:
            return upgrade(json.load(f))
    except (OSError, ValueError):
        return None


# ── Штат папки: кто в команде на самом деле ────────────────────────────────
def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def enabled_plugins(folder):
    """Включённые плагины: настройки пользователя, проекта и локальные поверх."""
    on = {}
    for path in (os.path.join(CLAUDE_DIR, 'settings.json'), os.path.join(folder, '.claude', 'settings.json'),
                 os.path.join(folder, '.claude', 'settings.local.json')):
        on.update(read_json(path).get('enabledPlugins') or {})
    return on


def plugin_dirs(folder):
    """Плагины, которые работают в этой папке: [(имя, путь, scope)]."""
    on = enabled_plugins(folder)
    out = []
    for key, entries in (read_json(os.path.join(CLAUDE_DIR, 'plugins', 'installed_plugins.json')).get('plugins') or {}).items():
        for e in entries:
            own = e.get('scope') == 'user' or e.get('projectPath') == folder
            if own and on.get(key) and e.get('installPath'):
                out.append((key.split('@')[0], e['installPath'], 'user' if e.get('scope') == 'user' else 'project'))
    return out


def read_agent(path, scope, plugin=''):
    """Файл агента: роль из заголовка, описание, инструкция."""
    try:
        with open(path) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    m = re.match(r'---\n(.*?)\n---\n?', text, re.S)
    head = dict(re.findall(r'^(\w+):\s*(.*)$', m.group(1), re.M)) if m else {}
    body = text[m.end():] if m else text
    role = re.search(r'^# (.+)$', body, re.M)
    key = head.get('name') or os.path.basename(path)[:-3]
    return {'type': plugin + ':' + key if plugin else key, 'role': role.group(1).strip() if role else '',
            'desc': head.get('description', '').strip('"\''), 'model': head.get('model', ''), 'prompt': ' '.join(body.split()),
            'scope': scope, 'plugin': plugin}


def agent_files(folder):
    """Агенты, описанные файлами: {тип: сведения}. Работают и до первой сессии, где они появятся в журнале."""
    found = {}
    sources = [(path, plugin, scope) for plugin, path, scope in plugin_dirs(folder)]
    sources += [(CLAUDE_DIR, '', 'user'), (os.path.join(folder, '.claude'), '', 'project')]
    for root, plugin, scope in sources:
        for path in sorted(glob.glob(os.path.join(glob.escape(root), 'agents', '*.md'))):
            info = read_agent(path, scope, plugin)
            if info:
                found[info['type']] = info
    return found


def names_in(folder):
    """Имена в папке. Её может не быть: журналы чистятся прямо во время обхода."""
    try:
        return os.listdir(folder)
    except OSError:
        return []


def mtime_of(path):
    """Время правки файла. Удалённый файл старше любого порога и выпадает из свежих сам."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def project_dirs(root):
    """Папки проектов Claude Code. Папки журналов может не быть: у кого есть только Codex."""
    return [p for p in (os.path.join(root, proj) for proj in names_in(root)) if os.path.isdir(p)]


def journal_dirs(folder):
    """Папки журналов проекта и его рабочих копий."""
    code = re.sub(r'[^A-Za-z0-9]', '-', folder)
    root = glob.escape(PROJECTS)
    return glob.glob(os.path.join(root, code)) + glob.glob(os.path.join(root, code + '--claude-worktrees-*'))


def roster_in(path):
    t = Tail(path)
    with open(path, 'rb') as f:
        for raw in f:
            if b'agent_listing_delta' in raw:
                try:
                    t.take(json.loads(raw))
                except Exception:
                    continue
    return t.roster


def newest_roster(folder):
    """Состав из самой свежей сессии папки: старая сессия не откатит штат назад."""
    paths = [p for d in journal_dirs(folder) for p in glob.glob(os.path.join(glob.escape(d), '*.jsonl'))]
    for path in sorted(paths, key=mtime_of, reverse=True):
        roster = roster_in(path)
        if roster:
            return roster
    return {}


def staff_of(folder, roster):
    """Штат: состав сессии и файлы агентов. Описание из файла полнее, чем из журнала."""
    files = agent_files(folder)
    merged = dict(roster)
    for t, info in files.items():
        merged[t] = merged.get(t) or info['desc']
    return merged, files


def usage_of(folder):
    """Сколько раз звали каждого агента и кого за кем: журналы чистятся, поэтому это только подсказка."""
    counts, pairs = {}, {}
    for d in journal_dirs(folder):
        for sub in glob.glob(os.path.join(glob.escape(d), '*', 'subagents')):
            metas = sorted(glob.glob(os.path.join(glob.escape(sub), 'agent-*.meta.json')), key=lambda p: (mtime_of(p), p))
            seq = [read_json(p).get('agentType') or 'general-purpose' for p in metas]
            for t in seq:
                counts[t] = counts.get(t, 0) + 1
            for a, b in zip(seq, seq[1:]):
                if a != b:
                    pairs[(a, b)] = pairs.get((a, b), 0) + 1
    return counts, pairs


def find_lead(folder, files):
    """Кто ведёт команду: скил проекта, который зовёт больше всего её агентов."""
    keys = {t.split(':')[-1] for t, i in files.items() if i['scope'] == 'project'}
    roots = [p for _, p, scope in plugin_dirs(folder) if scope == 'project'] + [os.path.join(folder, '.claude')]
    best, score = None, 2
    for root in roots:
        for path in glob.glob(os.path.join(glob.escape(root), 'skills', '*', 'SKILL.md')):
            try:
                with open(path) as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            n = sum('`%s`' % k in text or ':%s' % k in text for k in keys)
            if n > score:
                role = re.search(r'^# (.+)$', text, re.M)
                best, score = {'role': role.group(1).strip() if role else '', 'text': text}, n
    return best


def project_notes(folder, roster):
    """Строки CLAUDE.md про агентов: как устроена команда, словами автора."""
    try:
        with open(os.path.join(folder, 'CLAUDE.md')) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return ''
    keys = [t.split(':')[-1] for t in roster if t not in BUILTIN]
    wanted = re.compile('агент|agent|конвейер|pipeline|оркестр', re.I)
    rows = [row for row in text.splitlines() if wanted.search(row) or any(k in row for k in keys)]
    return '\n'.join(rows)[:3000]


def apply_manifest(old, folder):
    """Манифест проекта .claude/office.json — те же закреплённые столы, только в репозитории."""
    man = read_json(os.path.join(folder, '.claude', 'office.json'))
    if not man:
        return old
    office = dict(old or {'guests': {}, 'zones': []})
    pinned, zones = set(), []
    for z in man.get('zones') or []:
        desks = [dict(d, locked=True) for d in z.get('desks') or []]
        pinned.update(d.get('type') for d in desks)
        zones.append({'id': z.get('id') or zone_id(z.get('name', '')), 'name': str(z.get('name', '')).upper(), 'desks': desks})
    for z in office['zones']:
        desks = [d for d in z.get('desks') or [] if d['type'] not in pinned]
        if desks:
            zones.append(dict(z, desks=desks))
    office['zones'] = zones
    if man.get('title'):
        office.update(title=man['title'], titleLocked=True)
    if man.get('boss'):
        office['bossLocked'] = man['boss']
    return office


def seat(office, t, desc, files):
    """Посадить новичка без модели: к коллегам по плагину, иначе в отдел по роду работы."""
    lang, zones = office.get('lang', LANG), office['zones']
    info = files.get(t) or {}
    plugin = t.split(':')[0] if ':' in t else ''

    def sits_with(z):
        return sum(d['type'].split(':')[0] == plugin for d in z['desks'] if ':' in d['type'])

    def by_id(zid, name):
        for z in zones:
            if z.get('id') == zid:
                return z
        zones.append({'id': zid, 'name': name, 'desks': []})
        return zones[-1]

    team = any(i['scope'] == 'project' for i in files.values())
    if team and info.get('scope') == 'user':
        zone = by_id(zone_id(SIGNS[lang]['support']), SIGNS[lang]['support'])
    else:
        near = max(zones, key=sits_with, default=None) if plugin else None
        kind = kind_of(t, desc or '')
        same = [z for z in zones if z.get('id') == zone_id(kind)]
        zone = near if near and sits_with(near) else same[0] if same else by_id(zone_id(SIGNS[lang]['other']), SIGNS[lang]['other'])
    name = in_lang(info.get('role'), lang) or local_name(t, lang)
    desk = dress({'type': t, 'name': clip(name, 24)})
    if len(desk['name']) > 13:
        desk['short'] = local_name(t, lang) if len(local_name(t, lang)) <= 13 else clip(name, 13)
    zone['desks'].append(desk)


SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'boss': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'prop': {'type': 'string', 'enum': PROPS}},
                 'required': ['name', 'prop']},
        'zones': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string'}, 'name': {'type': 'string'},
            'desks': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'type': {'type': 'string'}, 'name': {'type': 'string'}, 'short': {'type': 'string'}, 'prop': {'type': 'string', 'enum': PROPS}},
                'required': ['type', 'name', 'short', 'prop']}}},
            'required': ['name', 'desks']}},
        'guests': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'type': {'type': 'string'}, 'name': {'type': 'string'}, 'prop': {'type': 'string', 'enum': PROPS}},
            'required': ['type', 'name', 'prop']}},
    },
    'required': ['title', 'boss', 'zones', 'guests'],
}

PROMPT = """Разложи команду ИИ-агентов проекта «{name}» ({folder}) по этажам пиксельного офиса так, как она работает на самом деле.
У главной сессии (руководителя) свой кабинет, остальные агенты — сотрудники по этажам-отделам.

Агенты:
{agents}

Как устроена работа:
{context}

Текущий офис:
{current}

{task}

Правила:
- Всё {language}: вывеска, отделы, должности.
- Смысл бери из инструкций агентов и описания работы выше, а не из их названий: этап в описании агента важнее похожего слова в типе.
- title: вывеска офиса заглавными, 1–3 слова, по сути проекта.
- boss: должность руководителя («Продюсер», «Тимлид», «Главред»), предмет из списка.
- zones: 2–5 отделов в порядке, в котором работа реально идёт от начала к выпуску. Агент сидит там, где делает основную работу. У отдела, который остался из текущего офиса, сохрани id.
  Название отдела — одно-два слова заглавными. В отделе 1–8 столов; не дроби отделы ради числа.
- Общие инструменты (источник — «общий инструмент») не смешивай с командой проекта.
- Каждый агент, кроме универсальных ({builtin}), — ровно за одним столом в zones; тип копируй дословно. В guests их не клади.
- guests — только универсальные агенты из списка выше, если они есть.
- name: полная должность, понятная без описания, не имя человека. Если у агента указана роль — она и есть должность.
- short: подпись на столе, до 13 знаков. Если name короче, повтори его.
- prop: предмет на столе, подходящий к работе, из списка в схеме."""
TASK_FULL = """Задача: пересобери офис по смыслу. Названия, которые уже подходят, не меняй без причины. Закреплённые столы (locked) останутся на местах."""
TASK_ADD = """Задача: штат пополнился. Новые агенты: {new}. Найди им места в текущем офисе. Остальных не перечисляй: их места не меняются."""


def agent_card(t, desc, info, calls):
    """Агент для модели: кто он, откуда и что ему поручено."""
    source = {'project': 'команда проекта', 'user': 'общий инструмент'}.get(info.get('scope'), 'неизвестен')
    rows = ['### ' + t, 'роль: %s; модель: %s; источник: %s; вызовов: %d' % (info.get('role') or '—', info.get('model') or '—', source, calls)]
    if desc:
        rows.append('описание: ' + clip(desc, 400))
    if info.get('prompt'):
        rows.append('инструкция: ' + clip(info['prompt'], 900))
    return '\n'.join(rows)


def office_prompt(folder, roster, files, old, mode):
    counts, pairs = usage_of(folder)
    agents = '\n\n'.join(agent_card(t, d, files.get(t) or {}, counts.get(t, 0)) for t, d in sorted(roster.items()))
    context = []
    lead = find_lead(folder, files)
    if lead:
        context.append('Скил руководителя:\n' + lead['text'][:3000])
    notes = project_notes(folder, roster)
    if notes:
        context.append('Из CLAUDE.md проекта:\n' + notes)
    top = sorted(pairs.items(), key=lambda kv: -kv[1])[:12]
    if top:
        context.append('Кого за кем звали:\n' + '\n'.join('%s → %s (%d)' % (a, b, n) for (a, b), n in top))
    current = {'title': (old or {}).get('title'), 'zones': [
        {'id': z.get('id'), 'name': z.get('name'), 'desks': [{k: d[k] for k in ('type', 'name', 'locked') if k in d} for d in z.get('desks') or []]}
        for z in (old or {}).get('zones') or []]} if old else 'офиса ещё нет'
    new = ', '.join((old or {}).get('pending') or [])
    return PROMPT.format(name=os.path.basename(folder), folder=folder, agents=agents or '(только универсальные)',
                         context='\n\n'.join(context) or 'описания нет, суди по агентам',
                         current=json.dumps(current, ensure_ascii=False), task=TASK_ADD.format(new=new) if mode == 'add' else TASK_FULL,
                         language=SIGNS[(old or {}).get('lang', LANG)]['language'], builtin=', '.join(sorted(BUILTIN)))


def find_claude():
    for p in [shutil.which('claude'), os.path.expanduser('~/.local/bin/claude'), '/opt/homebrew/bin/claude', '/usr/local/bin/claude']:
        if p and os.access(p, os.X_OK):
            return p
    for p in sorted(glob.glob(os.path.expanduser('~/.nvm/versions/node/*/bin/claude')), reverse=True):
        return p
    return None


class GenError(Exception):
    pass


def read_scutil(text):
    """Ответ scutil --proxy → переменные окружения."""
    keys = dict(re.findall(r'^\s*(\w+) : (.+)$', text, re.M))
    return {var: 'http://%s:%s' % (keys[kind + 'Proxy'], keys.get(kind + 'Port', '80'))
            for kind, var in (('HTTP', 'HTTP_PROXY'), ('HTTPS', 'HTTPS_PROXY'))
            if keys.get(kind + 'Enable') == '1' and keys.get(kind + 'Proxy')}


def system_proxy():
    try:
        return read_scutil(subprocess.run(['/usr/sbin/scutil', '--proxy'], capture_output=True, text=True, timeout=5).stdout)
    except (OSError, subprocess.SubprocessError):
        return {}


def shell_proxy():
    """Прокси из оболочки пользователя: launchd не читает её файлы, а claude без прокси может не достать до API."""
    try:
        out = subprocess.run([os.environ.get('SHELL') or '/bin/zsh', '-c', 'env'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    pairs = (row.split('=', 1) for row in out.splitlines() if '=' in row)
    return {k: v for k, v in pairs if k.lower().endswith('_proxy')}


def claude_env(exe):
    """Окружение claude: сервер под launchd получает его почти пустым, а системный прокси Node не видит."""
    env = dict(os.environ, PATH=os.path.dirname(exe) + ':' + os.environ.get('PATH', '/usr/bin:/bin'))
    if not any(k.lower() in ('https_proxy', 'all_proxy') for k in env):
        env.update(shell_proxy() or system_proxy())
    return env


def kill_job(job):
    """Убить claude вместе с его детьми: они держат трубы открытыми."""
    proc = job.get('proc')
    if proc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def read_stream(proc, prompt, job, got):
    """События claude по мере поступления: job знает, чего он ждёт, got — итог и последние строки."""
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except (OSError, ValueError):
        return
    try:
        for raw in proc.stdout:
            got['tail'] = raw
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get('type') == 'system' and e.get('subtype') == 'api_retry':
                job['retries'] = job.get('retries', 0) + 1
                continue
            job['heard'] = time.time()
            if e.get('type') == 'system' and e.get('subtype') == 'init':
                job['stage'] = 'answer'
            elif e.get('type') == 'result':
                got['answer'] = e
    except (OSError, ValueError):
        pass


def wait_for(proc, prompt, job):
    """Дождаться ответа, но не дольше лимитов; итог — словарь с answer, причина отказа — в why."""
    got = {'tail': '', 'why': ''}
    reader = threading.Thread(target=read_stream, args=(proc, prompt, job, got), daemon=True)
    began = job['heard'] = time.time()
    reader.start()
    while reader.is_alive() and not job.get('cancel'):
        now = time.time()
        if job.get('retries', 0) >= GEN_RETRIES or (job.get('stage') == 'answer' and now - job['heard'] > GEN_QUIET):
            got['why'] = NO_NETWORK
        elif now - began > GEN_TIMEOUT:
            got['why'] = ('claude не успел за %d с' % GEN_TIMEOUT if job.get('stage') == 'answer' else
                          'claude не запустился за %d с. Обычно он ждёт входа: запустите claude в терминале и войдите '
                          '(/login), а если macOS спросит доступ к связке ключей — разрешите' % GEN_TIMEOUT)
        if got['why']:
            break
        reader.join(0.1)
    kill_job(job)
    reader.join(2)  # труба могла остаться у того, кто сбежал из группы: ждать его незачем
    try:
        proc.wait(2)
        proc.stdout.close()
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return got


def ask_claude(folder, roster, files, old, mode, job):
    """Раскладка по смыслу — одним вызовом claude -p. Процесс лежит в job: его можно убить."""
    exe = find_claude()
    if not exe:
        raise GenError('не нашёл claude: нужен установленный Claude Code')
    prompt = office_prompt(folder, roster, files, old, mode)
    try:
        job['proc'] = subprocess.Popen(
            [exe, '-p', '--model', 'sonnet', '--tools', '', '--no-session-persistence', '--setting-sources', '',
             '--max-budget-usd', GEN_BUDGET, '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
             '--json-schema', json.dumps(SCHEMA),
             '--system-prompt', 'Ты раскладываешь агентов по пиксельному офису. Отвечай только JSON по схеме.'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
            cwd=DATA if os.path.isdir(DATA) else HERE, env=claude_env(exe))
    except OSError as e:
        raise GenError('claude не запустился: %s' % e)
    got = wait_for(job['proc'], prompt, job)
    if job.get('cancel'):
        return None
    if got['why']:
        raise GenError(got['why'])
    answer = got.get('answer')
    if not answer:
        raise GenError('claude завершился без ответа: %s' % clip(got['tail'] or 'пусто', 160))
    job['cost'] = answer.get('total_cost_usd')
    if answer.get('is_error'):
        raise GenError('claude: %s' % clip(answer.get('result') or 'ошибка без описания', 160))
    office = answer.get('structured_output')
    if not office or not office.get('zones'):
        raise GenError('claude вернул офис без отделов')
    return office


def backup_path(folder):
    return office_path(folder)[:-5] + '.prev.json'


def write_office(folder, office):
    """Новый файл — целиком, потом замена: прежний офис уходит в .prev.json, сбой его не задевает."""
    path = office_path(folder)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with OFFICE_LOCK:
        with open(tmp, 'w') as f:
            json.dump(office, f, ensure_ascii=False, indent=2)
        if os.path.exists(path):
            shutil.copy2(path, backup_path(folder))
        os.replace(tmp, path)


def changes(old, new):
    """Что поменялось для людей: перемещено, переименовано, добавлено."""
    def seats(office):
        return {d['type']: (z.get('name'), d.get('name')) for z in (office or {}).get('zones') or [] for d in z.get('desks') or []}
    before, after = seats(old), seats(new)
    both = [t for t in after if t in before]
    return {'moved': sum(before[t][0] != after[t][0] for t in both), 'renamed': sum(before[t][1] != after[t][1] for t in both),
            'added': len(after) - len(both)}


def run_office(folder, roster, job, mode='full'):
    """Фоновая генерация: при неудаче офис остаётся как был."""
    slot = False
    try:
        while not slot and not job.get('cancel'):
            slot = GEN_SLOT.acquire(timeout=0.1)
        if slot:
            job.update(stage='start', started=time.time())
            roster, files = staff_of(folder, roster)
            saved = load_office(folder)
            old = apply_manifest(saved, folder)
            office = ask_claude(folder, roster, files, old, mode, job)
            if office is not None:
                result = tidy(folder, roster, office, old, files, mode, find_lead(folder, files))
                job.update(summary=changes(saved, result), added=names_of(result, (saved or {}).get('pending') or []))
                write_office(folder, result)
        job['status'] = 'cancelled' if job.get('cancel') else 'done'
    except GenError as e:
        job['status'], job['error'] = 'failed', str(e)
    except Exception as e:  # генерация в потоке: ошибка не должна оставить «running» навсегда
        job['status'], job['error'] = 'failed', 'сбой: %s' % e
    finally:
        if slot:
            GEN_SLOT.release()
        sys.stderr.write('генерация офиса %s: %s $%s %s\n' % (folder, job['status'], job.get('cost', '?'), job.get('error', '')))
        job['ended'] = time.time()


def names_of(office, types):
    return [d['name'] for z in office['zones'] for d in z['desks'] if d['type'] in types]


def start_office(folder, roster, mode='full'):
    """Запустить генерацию; None — уже идёт. mode: full — пересобрать, add — посадить новичков."""
    with JOBS_LOCK:
        job = JOBS.get(folder)
        if job and job['status'] == 'running':
            return None
        job = JOBS[folder] = {'status': 'running', 'stage': 'queue', 'mode': mode, 'started': time.time(), 'ended': 0, 'error': ''}
    threading.Thread(target=run_office, args=(folder, roster, job, mode), daemon=True).start()
    return job


def cancel_office(folder):
    job = JOBS.get(folder)
    if not job or job['status'] != 'running':
        return False
    job['cancel'] = True
    kill_job(job)
    return True


def stop_jobs():
    """Сервер закрывается: claude не должен пережить его и потратить деньги впустую."""
    for job in list(JOBS.values()):
        if job['status'] == 'running':
            job['cancel'] = True
            kill_job(job)


def undo_office(folder):
    """Вернуть офис, который был до последней генерации."""
    prev = backup_path(folder)
    if not os.path.exists(prev):
        return False
    os.replace(prev, office_path(folder))
    job = JOBS.get(folder)
    if job and job['status'] == 'done':
        job['status'] = 'undone'
    return True


def job_view(folder):
    job = JOBS.get(folder) or {}
    return {'status': job.get('status', ''), 'stage': job.get('stage', ''), 'started': int(job.get('started', 0) * 1000),
            'ended': int(job.get('ended', 0) * 1000), 'error': job.get('error', ''),
            'canUndo': os.path.exists(backup_path(folder)), 'timeout': GEN_TIMEOUT, 'mode': job.get('mode', ''),
            'summary': job.get('summary'), 'added': job.get('added') or []}


def recent_rosters():
    """Папки недавних сессий и состав самой свежей из них."""
    rosters = {}
    for mtime, sid, pdir, path in session_files():
        t = tail(path)
        if t.home and t.roster and t.home not in rosters:
            rosters[t.home] = dict(t.roster)
    return rosters


def watch_staff():
    """Новичок садится за стол сразу, без модели; затем одна генерация уточняет его место и должность."""
    with LOCK:
        rosters = recent_rosters()
    for folder, roster in rosters.items():
        office = load_office(folder)
        running = JOBS.get(folder, {}).get('status') == 'running'
        if not office or office.get('auto') is False or running:
            continue  # офиса нет — автораскладка и так видит всех, тратить деньги без спроса не на что
        roster, files = staff_of(folder, roster)
        seated = {d['type'] for z in office['zones'] for d in z['desks']}
        new = [t for t in roster if t not in BUILTIN and t not in seated]
        if new:
            for t in new:
                seat(office, t, roster[t], files)
            office['pending'] = sorted(set(office.get('pending') or []) | set(new))
            write_office(folder, office)
        pending = tuple(office.get('pending') or [])
        if pending and AUTO_TRIED.get(folder) != pending and start_office(folder, roster, 'add') is not None:
            AUTO_TRIED[folder] = pending


def staff_loop():
    while True:
        time.sleep(STAFF_EVERY)
        try:
            watch_staff()
        except Exception as e:  # проверка в потоке: сбой не должен её остановить
            sys.stderr.write('проверка штата: %s\n' % e)


def tidy(folder, roster, raw, old=None, files=None, mode='full', lead=None):
    """Ответ модели — в файл офиса: чужие типы выкинуть, забытых посадить, руководителя — первым.
    Подпись выбирается по старшинству: закреплённая, из файла агента, прежняя, от модели, словарь.
    Подписи не на языке офиса отбрасываются. mode add двигает только новичков."""
    old, files = old or {}, files or {}
    lang = old.get('lang') or LANG
    signs = SIGNS[lang]
    fit = lambda text: in_lang(text, lang)
    was = {d['type']: d for z in old.get('zones') or [] for d in z.get('desks') or []}
    fresh = set(old.get('pending') or [])
    # что модель сказала про агента, где бы его ни посадила
    given = {}
    for d in [d for z in raw['zones'] for d in z.get('desks') or []] + list(raw.get('guests') or []):
        given.setdefault(d.get('type'), d)

    def label(t):
        before, said = was.get(t) or {}, given.get(t) or {}
        if before.get('locked') and fit(before.get('name')):
            name = fit(before['name'])
        else:
            kept = '' if t in fresh else fit(before.get('name'))
            name = fit((files.get(t) or {}).get('role')) or kept or fit(said.get('name')) or local_name(t, lang)
        marks = [name, before.get('short'), said.get('short'), before.get('name'), said.get('name'), local_name(t, lang)]
        short = next((m for m in map(fit, marks) if m and len(m) <= 13), clip(name, 13))
        return clip(name, 24), short

    def sit(t, prop=None):
        before = was.get(t) or {}
        desk = {k: before[k] for k in ('shirt', 'hair', 'skin', 'locked') if k in before}
        name, short = label(t)
        desk.update(type=t, name=name, prop=prop or before.get('prop'))
        if short != name:
            desk['short'] = short
        return dress(desk)

    zones = []

    def zone(zid, name):
        for z in zones:
            if z['id'] == zid:
                return z
        zones.append({'id': zid, 'name': name, 'desks': []})
        return zones[-1]

    seen = set()
    if mode == 'add':
        for z in old.get('zones') or []:
            zones.append(dict(z, desks=[d for d in z['desks'] if d['type'] not in fresh]))
            seen.update(d['type'] for d in zones[-1]['desks'])
    else:
        for z in old.get('zones') or []:
            pins = [d['type'] for d in z.get('desks') or [] if d.get('locked') and d['type'] != 'main']
            for t in pins:
                seen.add(t)
                zone(z['id'], z['name'])['desks'].append(sit(t))
    known = {z.get('id') for z in old.get('zones') or []}
    for z in raw['zones']:
        desks = []
        for d in z.get('desks') or []:
            t = d.get('type')
            if t in roster and t not in BUILTIN and t not in seen:
                seen.add(t)
                desks.append(sit(t, d.get('prop')))
        if desks:
            kinds = [kind_of(d['type'], roster.get(d['type']) or '') for d in desks]
            name = fit(z.get('name')) or fit(max(set(kinds), key=kinds.count)) or signs['other']
            zid = z.get('id') if z.get('id') in known else zone_id(name)
            zone(zid, clip(name, 24).upper())['desks'].extend(desks)
    rest = [t for t in roster if t not in BUILTIN and t not in seen]
    if mode == 'add':
        # модель забыла новичка: он остаётся там, куда его посадили сразу
        for z, source in zip(zones, old.get('zones') or []):
            z['desks'] += [d for d in source['desks'] if d['type'] in rest]
        rest = [t for t in rest if t not in was]
    if rest:
        zone(zone_id(signs['other']), signs['other'])['desks'] += [sit(t) for t in rest]
    if mode != 'add' and any(i['scope'] == 'project' for i in files.values()):
        tools = [d for z in zones for d in z['desks'] if (files.get(d['type']) or {}).get('scope') == 'user' and not d.get('locked')]
        if tools:
            for z in zones:
                z['desks'] = [d for d in z['desks'] if d not in tools]
            zone(zone_id(signs['support']), signs['support'])['desks'] += tools
    zones[:] = [z for z in zones if z['desks']]
    if not zones:
        zones.append({'id': 'главный', 'name': 'ГЛАВНЫЙ', 'desks': []})
    if mode == 'add':
        title = old.get('title')
    else:
        first = zones[0]['desks']
        pin = old.get('bossLocked') or {}
        before = was.get('main') or {}
        choices = [before.get('name') if before.get('locked') else '', pin.get('name'), (lead or {}).get('role'), before.get('name'),
                   raw['boss'].get('name')]
        boss = dress({'type': 'main', 'name': clip(next(filter(None, map(fit, choices)), signs['boss']), 13),
                      'prop': pin.get('prop') or raw['boss'].get('prop'), 'shirt': '#3b3f8f', **({'locked': True} if before.get('locked') else {})})
        # руководитель — в середине полного ряда, иначе первым
        first.insert(2 if len(first) == 4 else 0, boss)
        title = old.get('title') if old.get('titleLocked') else fit(raw.get('title')) or old.get('title') or os.path.basename(folder)
    if mode == 'add':
        guests = old.get('guests') or {}
    else:
        guests = {}
        for g in raw.get('guests') or []:
            t = g.get('type')
            if t in roster and t in BUILTIN:
                name = fit((old.get('guests') or {}).get(t, {}).get('name')) or fit(g.get('name')) or local_name(t, lang)
                guests[t] = {'name': clip(name, 13), 'prop': g.get('prop') if g.get('prop') in PROPS else 'cap'}
    keep = {k: old[k] for k in ('auto', 'titleLocked', 'bossLocked') if k in old}
    return dict(keep, v=OFFICE_V, lang=lang, folder=folder, title=clip(title, 28).upper(), zones=zones, guests=guests, generated='claude')


# ── Статистика: траты по часам из всех журналов ─────────────────────────────
HEAT_DAYS = 35  # сколько дней отдавать почасово, для карты «когда тратим»
ROLLOUT = re.compile(r'rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)\.jsonl(\.zst)?$')
# дешёвый отсев: json разбираем только у строк, важных для подсчёта
CODEX_WORDS = (b'"token_usage_record"', b'"token_count"', b'"turn_context"', b'"session_meta"', b'"thread_settings_applied"')


def find_zstd():
    for p in [shutil.which('zstd'), '/opt/homebrew/bin/zstd', '/usr/local/bin/zstd']:
        if p and os.access(p, os.X_OK):
            return p
    return None


def journal_lines(path, offset):
    """Целые строки журнала после байта offset: (строка, байт за ней). Сжатый читает zstd."""
    proc = None
    if path.endswith('.zst'):
        proc = subprocess.Popen([find_zstd(), '-dc', path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        f, pos = proc.stdout, 0
    else:
        f, pos = open(path, 'rb'), offset
        f.seek(offset)
    with f:
        for raw in f:
            pos += len(raw)
            if pos <= offset:
                continue
            if not raw.endswith(b'\n'):
                break  # строка ещё дописывается
            yield raw, pos
    if proc:
        proc.wait()


def blank_day(day):
    return {'day': day, 'usd': 0.0, 'models': {}, 'projects': {}, 'parts': [0.0, 0.0, 0.0, 0.0], 'tokens': 0,
            'tokParts': [0, 0, 0, 0], 'tokModels': {}, 'tokProjects': {}, 'sessions': 0, 'started': 0}


class Ledger:
    """Сводка трат по часам, моделям и папкам. Лежит на диске: Claude Code
    чистит журналы через месяц, а история трат остаётся."""
    IDS = 64  # ответ модели пишется кусками подряд — помним последние id файла
    VERSION = 2  # 2: токены по статьям в ячейке

    def __init__(self, path):
        self.path = path
        self.files = {}  # журнал → {offset, ids, days, main}
        # 'ГГГГ-ММ-ДДTЧЧ' → {папка: {модель: [$ вход, выход, чтение кэша, запись в кэш, токены, те же статьи в токенах]}}
        self.hours = {}
        self.rate = {}  # последние лимиты подписки Codex: at, plan, windows
        self.dirty = False
        self.saved_at = 0
        try:
            with open(path) as f:
                data = json.load(f)
            files, hours = data['files'], data['hours']
        except (OSError, ValueError, KeyError, TypeError):
            return
        # старая сводка без токенов по статьям: пока журналы на месте, читаем их заново,
        # иначе оставляем как есть — у той истории будет только общее число токенов
        if data.get('v') != self.VERSION and all(os.path.exists(f) for f in files):
            self.dirty = True
            return
        for projects in hours.values():
            for models in projects.values():
                for cell in models.values():
                    cell.extend([0] * (9 - len(cell)))
        self.files, self.hours, self.rate = files, hours, data.get('limits') or {}

    def scan(self, root=None, codex=None):
        if root is None:
            root, codex = PROJECTS, CODEX_SESSIONS
        if codex:
            self.scan_codex(codex)
        for pdir in project_dirs(root):
            for name in names_in(pdir):
                if name.endswith('.jsonl'):
                    self.feed(os.path.join(pdir, name), True)
                    continue
                sub = os.path.join(pdir, name, 'subagents')
                for agent in names_in(sub):
                    if agent.endswith('.jsonl'):
                        self.feed(os.path.join(sub, agent), False)
        return self

    def scan_codex(self, root):
        """Журналы Codex остывают в .jsonl.zst и оживают обратно, поэтому ключ — имя без .zst."""
        journals = {}
        for folder, _, names in os.walk(root):
            for name in names:
                if ROLLOUT.match(name):
                    key = os.path.join(folder, name[:-4] if name.endswith('.zst') else name)
                    if key not in journals or not name.endswith('.zst'):
                        journals[key] = os.path.join(folder, name)
        for key, path in journals.items():
            self.feed_codex(key, path)

    def feed_codex(self, key, path):
        rec = self.files.setdefault(key, {'offset': 0, 'ids': [], 'days': [], 'main': True, 'provider': 'codex', 'meter': {}, 'cwd': ''})
        packed = path.endswith('.zst')
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if (rec.get('packed') if packed else rec['offset']) == size or (packed and not find_zstd()):
            return
        for raw, end in journal_lines(path, rec['offset']):
            if any(word in raw for word in CODEX_WORDS):
                self.safely(self.take_codex, rec, raw)
            rec['offset'] = end
        if packed:
            rec['packed'] = size
        self.dirty = True

    def take_codex(self, rec, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        kind, p, at = d.get('type'), d.get('payload') or {}, ms(d.get('timestamp'))
        usage = codex_usage(rec['meter'], d)
        if kind == 'session_meta':
            if not rec['cwd']:
                rec['cwd'] = p.get('cwd') or '?'
                rec['main'] = not codex_origin(p)[1]
            return
        if kind == 'turn_context':
            rec['cwd'] = p.get('cwd') or rec['cwd']
        self.note_limits(p.get('rate_limits'), d.get('timestamp') or '')
        if usage and at:
            self.add(rec, at, rec['cwd'], codex_model(rec['meter']), usage)

    def note_limits(self, limits, ts):
        if not limits or limits.get('limit_id') not in (None, 'codex') or ts <= self.rate.get('at', ''):
            return
        windows = [{'minutes': w.get('window_minutes'), 'used': w.get('used_percent'), 'resets': w.get('resets_at')}
                   for w in (limits.get('primary'), limits.get('secondary')) if w]
        if windows:
            self.rate = {'at': ts, 'plan': limits.get('plan_type'), 'windows': windows}

    def feed(self, path, main):
        rec = self.files.setdefault(path, {'offset': 0, 'ids': [], 'days': [], 'main': main})
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < rec['offset']:
            rec['offset'] = size  # журнал укоротили: прочитанное уже в сводке, с начала — удвоили бы траты
        if size == rec['offset']:
            return
        try:
            for raw, rec['offset'] in journal_lines(path, rec['offset']):
                if b'"usage"' in raw:  # дешёвый отсев: json разбираем только у ответов модели
                    self.safely(self.take, rec, raw)
                self.dirty = True
        except OSError:
            pass  # журнал удалили между проверкой и чтением

    def safely(self, take, rec, raw):
        """Чужая запись теряется одна, а не вместе со всей статистикой."""
        try:
            take(rec, raw)
        except Exception:
            pass

    def take(self, rec, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        msg = d.get('message') if d.get('type') == 'assistant' else None
        usage = msg.get('usage') if isinstance(msg, dict) else None
        at = ms(d.get('timestamp'))
        if not usage or not at:
            return
        mid = msg.get('id')
        if mid:
            if mid in rec['ids']:
                return
            rec['ids'].append(mid)
            del rec['ids'][:-self.IDS]
        self.add(rec, at, d.get('cwd'), msg.get('model') or '?', usage)

    def add(self, rec, at, cwd, model, usage):
        parts, toks = cost_parts(model, usage), token_parts(usage)
        tokens = sum(toks)
        if not tokens:
            return
        hour = time.strftime('%Y-%m-%dT%H', time.localtime(at / 1000))
        project = folder_name(home_of(cwd)) or '?'
        cell = self.hours.setdefault(hour, {}).setdefault(project, {}).setdefault(model, [0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, 0])
        for i, n in enumerate(parts + [tokens] + toks):
            cell[i] += n
        if rec['main'] and hour[:10] not in rec['days']:
            rec['days'].append(hour[:10])

    def save(self, every=0):
        if not self.dirty or time.time() - self.saved_at < every:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'v': self.VERSION, 'files': self.files, 'hours': self.hours, 'limits': self.rate}, f, separators=(',', ':'))
        os.replace(tmp, self.path)
        self.dirty, self.saved_at = False, time.time()

    def report(self, now=None, provider=None):
        """Дни подряд от первой записи до сегодня; почасово — последние HEAT_DAYS дней.
        provider — только Claude Code или только Codex."""
        from datetime import date, timedelta
        now = now or time.time()
        since_24 = time.strftime('%Y-%m-%dT%H', time.localtime(now - 23 * 3600))
        heat_from = day_of((now - HEAT_DAYS * 86400) * 1000)
        days, hours, last24, first = {}, [], 0.0, {}
        for hour, projects in sorted(self.hours.items()):
            spent = 0.0
            for project, models in projects.items():
                for model, cell in models.items():
                    who = provider_of(model)
                    first.setdefault(who, hour[:10])
                    if provider and who != provider:
                        continue
                    row = days.setdefault(hour[:10], blank_day(hour[:10]))
                    usd = sum(cell[:4])
                    spent += usd
                    row['models'][model] = row['models'].get(model, 0.0) + usd
                    row['projects'][project] = row['projects'].get(project, 0.0) + usd
                    row['parts'] = [a + b for a, b in zip(row['parts'], cell[:4])]
                    row['tokens'] += cell[4]
                    row['tokParts'] = [a + b for a, b in zip(row['tokParts'], cell[5:9])]
                    row['tokModels'][model] = row['tokModels'].get(model, 0) + cell[4]
                    row['tokProjects'][project] = row['tokProjects'].get(project, 0) + cell[4]
            if hour[:10] in days:
                days[hour[:10]]['usd'] += spent
            if hour >= since_24:
                last24 += spent
            if hour[:10] >= heat_from and spent:
                hours.append([hour[:10], int(hour[11:]), round(spent, 4)])
        for rec in self.files.values():
            if provider and rec.get('provider', 'claude') != provider:
                continue
            for day in rec['days'] if rec['main'] else []:
                days.setdefault(day, blank_day(day))['sessions'] += 1
            if rec['main'] and rec['days']:
                days[min(rec['days'])]['started'] += 1
        out = []
        if days:
            d, end = date.fromisoformat(min(days)), date.fromisoformat(day_of(now * 1000))
            while d <= end:
                out.append(days.get(d.isoformat()) or blank_day(d.isoformat()))
                d += timedelta(days=1)
        for row in out:
            row['usd'] = round(row['usd'], 4)
            row['parts'] = [round(x, 4) for x in row['parts']]
            for k in ('models', 'projects'):
                row[k] = {name: round(usd, 4) for name, usd in row[k].items()}
        return {'since': out[0]['day'] if out else '', 'last24': round(last24, 4), 'days': out, 'hours': hours,
                'first': first, 'limits': self.rate}


LEDGER = None
STATS_LOCK = threading.Lock()  # статистика копится отдельно от офиса: первый проход долгий


def stats(provider=None):
    global LEDGER
    with STATS_LOCK:
        if LEDGER is None:
            LEDGER = Ledger(os.path.join(DATA, 'stats.json'))
        LEDGER.scan()
        LEDGER.save(every=60)
        return LEDGER.report(provider=provider)


TAILS = {}


def tail(path, kind=Tail):
    t = TAILS.get(path)
    if t is None:
        t = TAILS[path] = kind(path)
    return t.feed()


def session_files():
    now = time.time()
    out = []
    for pdir in project_dirs(PROJECTS):
        for name in names_in(pdir):
            if not name.endswith('.jsonl'):
                continue
            path = os.path.join(pdir, name)
            sub = os.path.join(pdir, name[:-6], 'subagents')
            mtime = max([mtime_of(path)] + [mtime_of(os.path.join(sub, f)) for f in names_in(sub)])
            if os.path.exists(path) and now - mtime < RECENT_S:
                out.append((mtime, name[:-6], pdir, path))
    out.sort(reverse=True)
    return out


def project_name(pdir, cwd):
    if cwd:
        return folder_name(home_of(cwd)) or cwd
    return os.path.basename(pdir).split('-')[-1]


def agent_journals(pdir, sid):
    sub = os.path.join(pdir, sid, 'subagents')
    return [(name[6:-6], os.path.join(sub, name)) for name in names_in(sub) if name.startswith('agent-') and name.endswith('.jsonl')]


def agents_of(pdir, sid, now_ms):
    agents = []
    for aid, path in agent_journals(pdir, sid):
        try:
            with open(path[:-6] + '.meta.json') as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        t = tail(path)
        idle = now_ms - (mtime_of(path) * 1000)
        working = not meta.get('finished') and idle < AGENT_IDLE_S * 1000
        agents.append({
            'id': aid,
            'type': meta.get('agentType') or 'general-purpose',
            'desc': clip(meta.get('description', ''), 40),
            'isWorking': working,
            'activity': t.activity or clip(meta.get('description', ''), 40),
            'startedAt': t.first,
            'endedAt': 0 if working else t.last,
            'calls': t.calls,
            'cost': round(t.cost, 4),
            'today': round(today_cost(t), 4),
            'model': t.model,
            'tool': t.tool,
            'at': t.activity_at or t.last,
            'finished': bool(meta.get('finished')),
            'events': t.events,
            'runs': max(1, t.requests),
            'spans': t.worked(now_ms, not meta.get('finished')),
        })
    agents.sort(key=lambda a: a['startedAt'])
    return agents


def title_of(t, sid):
    return t.title or clip(t.prompt, 50) or sid[:8]


def live_ids():
    """Кто сейчас запущен: id в приложении → id журнала. Рядом лежат *.key с токенами — их не трогаем."""
    out = {}
    for name in names_in(SESSIONS_DIR):
        if name.endswith('.json'):
            d = read_json(os.path.join(SESSIONS_DIR, name))
            if isinstance(d, dict) and d.get('hostSessionId') and d.get('sessionId'):
                out[d['hostSessionId']] = d['sessionId']
    return out


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError, TypeError):
        return False
    return bool(pid)


def running_ids():
    """Журналы сессий с живым процессом. None: файл дописывается, судить нельзя."""
    out = set()
    for name in names_in(SESSIONS_DIR):
        if name.endswith('.json'):
            d = read_json(os.path.join(SESSIONS_DIR, name))
            if not isinstance(d, dict) or not d.get('sessionId'):
                return None
            if process_alive(d.get('pid')):
                out.add(d['sessionId'])
    return out


SEEN_ALIVE = set()  # сессии, чей процесс застали: только про них можно сказать, что его не стало


def note_closed(tails, now_ms):
    """Журнал не пишет, что сессию закрыли, зато исчезает её процесс."""
    alive = running_ids()
    if alive is None:
        return
    for sid in SEEN_ALIVE - alive:
        SEEN_ALIVE.discard(sid)
        if sid in tails:
            t, pdir, _ = tails[sid]
            t.close(now_ms)
            for _, path in agent_journals(pdir, sid):
                tail(path).close(now_ms)
    SEEN_ALIVE.update(alive & set(tails))


def claude_tails():
    """Сессии Claude за сутки: id → (хвост, папка журналов, mtime)."""
    tails = {sid: (tail(path), pdir, mtime) for mtime, sid, pdir, path in session_files()}
    note_closed(tails, time.time() * 1000)
    return tails


def task_tree(tails):
    """Родитель каждой сессии. Связь — заметка приложения в журнале родителя;
    ребёнок, чей родитель уже закрыт, пристаёт к последней чужой сессии своей папки."""
    hosts = live_ids()
    parent = {}
    for sid, (t, _, _) in tails.items():
        for host in t.linked:
            kid = hosts.get(host)
            if kid in tails and kid != sid and kid not in parent:
                parent[kid] = sid
    for sid, (t, _, _) in tails.items():
        if not t.child or sid in parent:
            continue
        others = [(o.last, s) for s, (o, _, _) in tails.items() if s != sid and not o.child and o.home == t.home and s not in parent]
        if others:
            parent[sid] = max(others)[1]
    return parent


def root_of(sid, parent):
    seen = {sid}
    while sid in parent and parent[sid] not in seen:
        sid = parent[sid]
        seen.add(sid)
    return sid


def task_of(sid, tails, parent):
    """Сессии задачи: корень, дальше порождённые в порядке старта."""
    root = root_of(sid, parent)
    kids = sorted((s for s in tails if s != root and root_of(s, parent) == root), key=lambda s: tails[s][0].first)
    return [root] + kids


def sessions():
    tails = claude_tails()  # раньше часов: закрытие сессии отмечается при чтении
    now = time.time()
    out = []
    parent = task_tree(tails)
    for sid, (t, pdir, mtime) in tails.items():
        live = now - mtime < LIVE_S
        agents = agents_of(pdir, sid, now * 1000)
        working = sum(a['isWorking'] for a in agents) + (1 if live and t.busy else 0)
        shells = t.running_shells(now * 1000)
        out.append({
            'id': sid,
            'provider': 'claude',
            'title': title_of(t, sid),
            'project': project_name(pdir, t.cwd),
            'at': int(mtime * 1000),
            'isLive': live or shells > 0,
            'working': working,
            **clock_of([t], agents, now * 1000),
            'shells': shells,
            'agents': len(agents),
            'cost': round(t.cost + sum(a['cost'] for a in agents), 2),
            'today': round(today_cost(t) + sum(a['today'] for a in agents), 2),
            'folder': t.home,
            'hasOffice': bool(t.home) and os.path.exists(office_path(t.home)),
            'task': root_of(sid, parent),
            'parent': parent.get(sid, ''),
            'branch': t.branch,
        })
    out += codex_sessions(now)
    out.sort(key=lambda s: s['at'], reverse=True)
    return out


CODEX_TITLES = {'mtime': 0, 'names': {}}


def codex_titles():
    """Названия тредов: Codex дописывает их в session_index.jsonl."""
    try:
        mtime = os.path.getmtime(CODEX_INDEX)
        if mtime != CODEX_TITLES['mtime']:
            names = {}
            with open(CODEX_INDEX) as f:
                for raw in f:
                    try:
                        row = json.loads(raw)
                        names[row['id']] = row.get('thread_name')
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue  # одна странная запись не стоит всех названий
            CODEX_TITLES.update(mtime=mtime, names=names)
    except (OSError, ValueError):
        return {}
    return CODEX_TITLES['names']


def codex_threads(now):
    """Журналы Codex с записями за сутки: {файл: хвост} главных сессий и дети по id родителя."""
    mains, kids = {}, {}
    for folder, _, names in os.walk(CODEX_SESSIONS):
        for name in names:
            m = ROLLOUT.match(name)
            path = os.path.join(folder, name)
            if not m or m.group(2) or now - mtime_of(path) >= RECENT_S:
                continue  # сжатые журналы давно остыли
            t = tail(path, CodexTail)
            if (now * 1000 - t.last) >= RECENT_S * 1000:
                continue  # файл тронут, а записей нет
            if t.parent:
                kids.setdefault(t.parent, []).append(t)
            elif not t.role:
                mains[m.group(1)] = t
    return mains, kids


def codex_agents(kids, now_ms):
    """Дети сессии — её агенты. Контролёр один, сколько бы ревью он ни провёл."""
    agents = {}
    for t in sorted(kids, key=lambda t: t.first):
        working = t.busy and now_ms - t.last < AGENT_IDLE_S * 1000
        a = agents.get(t.role if t.role == 'guardian' else t.thread)
        if a is None:
            a = agents[t.role if t.role == 'guardian' else t.thread] = {
                'id': t.thread, 'type': t.role, 'desc': clip(t.nickname or t.prompt or 'проверяет действия', 40), 'isWorking': False,
                'activity': '', 'startedAt': t.first, 'endedAt': 0, 'calls': 0, 'cost': 0.0, 'today': 0.0, 'model': '', 'tool': '',
                'at': 0, 'finished': False, 'events': [], 'runs': 0, 'spans': []}
        a['isWorking'] = a['isWorking'] or working
        a['activity'] = t.activity or a['activity'] or a['desc']
        a['calls'] += t.calls
        a['cost'] = round(a['cost'] + t.cost, 4)
        a['today'] = round(a['today'] + today_cost(t), 4)
        a['model'], a['tool'] = t.model or a['model'], t.tool or a['tool']
        a['at'] = max(a['at'], t.activity_at or t.last)
        a['endedAt'] = 0 if a['isWorking'] else max(a['endedAt'], t.last)
        a['finished'] = not a['isWorking']
        a['events'] = (a['events'] + t.events)[-LOG_MAX:]
        a['runs'] += max(1, t.requests)
        a['spans'] += t.worked(now_ms)
    return list(agents.values())


def codex_sessions(now):
    titles = codex_titles()
    mains, kids = codex_threads(now)
    out = []
    for sid, t in mains.items():
        agents = codex_agents(kids.get(t.thread, []), now * 1000)
        live = now * 1000 - t.last < LIVE_S * 1000
        out.append({
            'id': sid,
            'provider': 'codex',
            'title': titles.get(t.thread) or clip(t.prompt, 50) or sid[:8],
            'project': project_name('', t.cwd),
            'at': t.last,
            'isLive': live,
            'working': sum(a['isWorking'] for a in agents) + (1 if live and t.busy else 0),
            **clock_of([t], agents, now * 1000),
            'agents': len(agents),
            'cost': round(t.cost + sum(a['cost'] for a in agents), 2),
            'today': round(today_cost(t) + sum(a['today'] for a in agents), 2),
            'folder': t.home,
            'hasOffice': False,
            'task': sid,
            'parent': '',
            'branch': '',
        })
    return out


def codex_office(t, agents):
    """Столы Codex — по тому, кто работал в сессии: генерировать тут нечего, ролей он не объявляет."""
    office = auto_office(t.home or '?', {a['type']: '' for a in agents}, '#1f6f8f')
    office.update(saved=True, job=None, generating=False, provider='codex', accent=CODEX_ACCENT)
    return office


def session_view(office, roster, called):
    """Штат один на папку, а показываем столы тех, кого эта сессия может позвать или уже звала."""
    if not roster:
        return office
    keep = set(roster) | set(called) | {'main'}
    office['guests'] = guests_of(list(roster) + [t for t in called if t not in roster], office.get('lang', LANG), office.get('guests'))
    zones = [dict(z, desks=[d for d in z['desks'] if d['type'] in keep]) for z in office['zones']]
    total = sum(len(z['desks']) for z in office['zones'])
    office['zones'] = [z for z in zones if z['desks']]
    office['hidden'] = total - sum(len(z['desks']) for z in office['zones'])
    return office


def office_for(t, called=(), roster=None):
    roster = t.roster if roster is None else roster
    saved = load_office(t.home) if t.home else None
    office = session_view(saved, roster, called) if saved else auto_office(t.home or '?', roster)
    office['saved'] = bool(saved)
    office['job'] = job_view(t.home) if t.home else None
    office['generating'] = bool(office['job']) and office['job']['status'] == 'running'
    return office


def find_tail(sid):
    for mtime, s, pdir, path in session_files():
        if s == sid:
            return tail(path)
    return None


def head_of(t, sid, live, busy):
    """Руководитель сессии: то же, что producer, плюс кто он."""
    return {'id': sid, 'title': title_of(t, sid), 'isLive': live, 'isBusy': busy, 'activity': t.activity if busy else '',
            'at': t.activity_at, 'calls': t.calls, 'cost': round(t.cost, 4), 'model': t.model, 'tool': t.tool, 'branch': t.branch, 'since': t.turn or t.first}


def room(tails, agents, office, heads, now_ms):
    """Состояние офиса задачи: руководители её сессий, агенты, общий журнал.
    Когда сессий несколько, записи порождённых помечены их номером: ② первая порождённая; корень без метки."""
    mark = (lambda i: MARKS[min(i, len(MARKS)) - 1] + ' ' if i > 1 else '') if len(heads) > 1 else (lambda i: '')
    log = []
    for i, t in enumerate(tails, 1):
        log += [(at, 'producer', mark(i) + text) for at, text in t.events]
    for a in agents:
        m = mark(a['session'])
        log.append((a['startedAt'], a['type'], m + 'взял задачу: ' + a['desc']))
        log += [(at, a['type'], m + text) for at, text in a['events']]
        if not a['isWorking']:
            log.append((a['endedAt'], a['type'], m + 'сдал работу'))
    log.sort(key=lambda x: x[0])
    head = heads[0]
    clock = clock_of(tails, agents, now_ms)
    for h, t in zip(heads, tails):
        h.update(work_clock(t.worked(now_ms), now_ms), requests=t.requests)
    for a in agents:
        a.update(own_clock(a, now_ms))
        del a['events'], a['spans']
    shells = [dict(s, session=i) for i, t in enumerate(tails, 1) for s in t.shells.values() if shell_shown(s, now_ms)]
    # упавшие первыми: в плотной серверной они не должны уйти за край
    shells.sort(key=lambda s: ({'failed': 0, 'running': 1}.get(s['status'], 2), s['start']))
    return {
        'agents': agents,
        'producer': {k: head[k] for k in ('isBusy', 'activity', 'at', 'calls', 'cost', 'model', 'tool')},
        'sessions': heads,
        'cost': round(sum(h['cost'] for h in heads) + sum(a['cost'] for a in agents), 4),
        'office': office,
        'log': [{'at': at, 'who': who, 'text': clip(text)} for at, who, text in log[-LOG_MAX:]],
        'since': head['since'],
        **clock,
        'shells': shells,
    }


def state(sid):
    tails = claude_tails()  # раньше часов: закрытие сессии отмечается при чтении
    now = time.time()
    if sid in tails:
        # офис задачи — у её корня; порождённая сессия по клику показывает только своё
        parent = task_tree(tails)
        members = task_of(sid, tails, parent) if root_of(sid, parent) == sid else [sid]
        heads, agents, roster, chain = [], [], {}, []
        for i, s in enumerate(members, 1):
            t, pdir, mtime = tails[s]
            live = now - mtime < LIVE_S
            heads.append(head_of(t, s, live or t.running_shells(now * 1000) > 0, t.busy and live))
            own = agents_of(pdir, s, now * 1000)
            for a in own:
                a['session'] = i
            agents += own
            roster.update(t.roster)
            chain.append(t)
        agents.sort(key=lambda a: a['startedAt'])
        return room(chain, agents, office_for(chain[0], [a['type'] for a in agents], roster), heads, now * 1000)
    mains, kids = codex_threads(now)
    t = mains.get(sid)
    if t is None:
        return None
    agents = codex_agents(kids.get(t.thread, []), now * 1000)
    for a in agents:
        a['session'] = 1
    live = now * 1000 - t.last < LIVE_S * 1000
    return room([t], agents, codex_office(t, agents), [head_of(t, sid, live, t.busy and live)], now * 1000)


def shot_name(raw):
    return re.sub(r'[^\w -]+', '', raw).strip(' -')[:80].strip() or 'agent-office'


def save_shot(data, name='agent-office'):
    os.makedirs(SHOTS, exist_ok=True)
    stamp = '%s %s' % (name, time.strftime('%Y-%m-%d %H-%M-%S'))
    n = 1
    while True:
        path = os.path.join(SHOTS, '%s%s.png' % (stamp, '-%d' % n if n > 1 else ''))
        try:
            with open(path, 'xb') as f:
                f.write(data)
            return path
        except FileExistsError:
            n += 1


def trusted_hosts(host):
    """Имена сервера. На 0.0.0.0 его открыли всей сети сами: по какому имени придут, заранее неизвестно."""
    return None if host in ('0.0.0.0', '::', '') else {'localhost', '127.0.0.1', host}


class Handler(SimpleHTTPRequestHandler):
    timeout = 30  # с: зависший клиент не держит поток вечно

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        # страница и office.js меняются при правках — кэш браузера не нужен
        if not self.path.startswith('/api/'):
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def send_json(self, body, code=200):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        """Запрос своей страницы. Чужой сайт приходит с доменом, направленным на 127.0.0.1 (свой Host),
        или прямо со своей страницы (свой Origin): ни читать сессии, ни тратить деньги ему нельзя."""
        if HOSTS is None:
            return True
        host = re.sub(r':\d+$', '', self.headers.get('Host', '')).lower()
        origin = self.headers.get('Origin')
        return host in HOSTS and (origin is None or urlparse(origin).hostname in HOSTS)

    def post_shot(self):
        # image/png чужая страница без preflight не пришлёт, а отвечать на него здесь некому
        if self.headers.get('Content-Type') != 'image/png':
            return self.send_json({'error': 'нужен image/png'}, 415)
        try:
            size = int(self.headers.get('Content-Length', ''))
        except ValueError:
            size = -1
        if size < 0:
            return self.send_json({'error': 'нет размера'}, 400)
        if size > SHOT_LIMIT:
            return self.send_json({'error': 'скриншот слишком большой'}, 413)
        data = self.rfile.read(size)
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            return self.send_json({'error': 'это не PNG'}, 400)
        try:
            name = shot_name(parse_qs(urlparse(self.path).query).get('name', [''])[0])
            return self.send_json({'path': save_shot(data, name)}, 201)
        except OSError as e:
            return self.send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not self.trusted():
            return self.send_json({'error': 'чужой запрос'}, 403)
        try:
            return self.post()
        except Exception as e:  # как в do_GET: ошибка — ответ, а не оборванное соединение
            return self.send_json({'error': str(e)}, 500)

    def post(self):
        # /api/office — собрать заново, /api/office/cancel — отменить, /api/office/undo — вернуть прежний
        url = urlparse(self.path)
        if url.path == '/api/shot':
            return self.post_shot()
        action = {'/api/office': 'start', '/api/office/cancel': 'cancel', '/api/office/undo': 'undo'}.get(url.path)
        if not action:
            return self.send_json({'error': 'нет такого адреса'}, 404)
        sid = parse_qs(url.query).get('s', [''])[0]
        with LOCK:
            t = find_tail(sid)
            if not t or not t.home:
                return self.send_json({'error': 'у сессии не нашлась папка'}, 404)
            folder, roster = t.home, dict(t.roster)
        if action == 'start' and not start_office(folder, newest_roster(folder) or roster):
            return self.send_json({'error': 'офис уже собирается'}, 409)
        if action == 'cancel' and not cancel_office(folder):
            return self.send_json({'error': 'отменять нечего'}, 409)
        if action == 'undo' and not undo_office(folder):
            return self.send_json({'error': 'прежнего офиса нет'}, 409)
        return self.send_json(job_view(folder), 202 if action == 'start' else 200)

    def do_GET(self):
        if not self.trusted():
            return self.send_json({'error': 'чужой запрос'}, 403)
        url = urlparse(self.path)
        try:
            if url.path == '/api/sessions':
                with LOCK:
                    body = sessions()
                return self.send_json(body)
            if url.path == '/api/stats':
                p = parse_qs(url.query).get('p', [''])[0]
                return self.send_json(stats(p if p in ('claude', 'codex') else None))
            if url.path == '/api/state':
                with LOCK:
                    st = state(parse_qs(url.query).get('s', [''])[0])
                return self.send_json(st if st else {'error': 'нет такой сессии'}, 200 if st else 404)
        except Exception as e:  # офис только смотрит: ошибка не роняет сервер
            return self.send_json({'error': str(e)}, 500)
        return super().do_GET()


def on_term(signum, frame):
    stop_jobs()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)  # код выхода тот же: приложение строки меню по нему решает, поднимать ли сервер


def main():
    global DATA, SHOTS, HOSTS
    import argparse
    ap = argparse.ArgumentParser(description='Agent Office: пиксельный офис агентов Claude Code и Codex')
    ap.add_argument('--port', type=int, default=8767)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--data', default=DATA, help='где хранить офисы папок')
    ap.add_argument('--shots', default=SHOTS, help='куда класть скриншоты офиса')
    ap.add_argument('--watch-parent', action='store_true', help='выйти, когда умрёт родительский процесс')
    args = ap.parse_args()
    DATA = os.path.expanduser(args.data)
    SHOTS = os.path.expanduser(args.shots)
    HOSTS = trusted_hosts(args.host)
    if args.watch_parent:
        ppid = os.getppid()

        def watch():
            while parent_alive(ppid):
                time.sleep(2)
            stop_jobs()
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)  # claude живёт в своей сессии: Ctrl-C до него не дойдёт
    threading.Thread(target=staff_loop, daemon=True).start()
    threading.Thread(target=stats, daemon=True).start()  # первый проход по журналам — заранее
    print('Agent Office: http://%s:%d' % (args.host, args.port), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
