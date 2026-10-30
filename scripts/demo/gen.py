#!/usr/bin/env python3
"""Вымышленные журналы Claude Code и Codex для скриншотов README.
  gen.py <root>          собрать данные заново
  gen.py <root> --pulse  держать «работающих» агентов живыми, дописывая журналы
Запускается из shoot.sh.
"""
import json
import os
import random
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(sys.argv[1])
PULSE = '--pulse' in sys.argv
CLAUDE = os.path.join(ROOT, 'claude')
CODEX = os.path.join(ROOT, 'codex')
DATA = os.path.join(ROOT, 'data')
HOME = '/home/demo/'
BAKERY = HOME + 'pixel-bakery'
R = random.Random(7)


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def code(folder):
    return ''.join(c if c.isalnum() else '-' for c in folder)


def put(path, rows, mode='a'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def usage(k=1.0):
    return {'input_tokens': int(R.randint(40, 400) * k), 'output_tokens': int(R.randint(300, 2600) * k),
            'cache_read_input_tokens': int(R.randint(20000, 120000) * k),
            'cache_creation_input_tokens': int(R.randint(1000, 14000) * k)}


def said(at, cwd, tool, inp, model, k=1.0, stop='tool_use'):
    content = [{'type': 'tool_use', 'name': tool, 'input': inp}] if tool else [{'type': 'text', 'text': 'готово'}]
    return {'type': 'assistant', 'timestamp': iso(at), 'cwd': cwd,
            'message': {'id': uuid.uuid4().hex, 'model': model, 'usage': usage(k), 'content': content, 'stop_reason': stop}}


def asked(at, cwd, text):
    return {'type': 'user', 'timestamp': iso(at), 'cwd': cwd, 'message': {'role': 'user', 'content': text}}


TEAM = {
    'bakery:researcher': ('Исследователь', 'magnifier', 'гуглит конкурентов'),
    'bakery:analyst': ('Аналитик', 'chart', 'считает воронку'),
    'bakery:frontend': ('Фронтендер', 'tablet', 'собирает корзину'),
    'bakery:backend': ('Бэкендер', 'box', 'пишет оплату'),
    'bakery:designer': ('Дизайнер', 'beret', 'рисует логотип'),
    'bakery:tester': ('Тестировщик', 'clipboard', 'гоняет тесты'),
    'bakery:reviewer': ('Ревьюер', 'glasses', 'читает дифф'),
    'bakery:writer': ('Техписатель', 'script', 'пишет README'),
}


def office():
    def desk(t, shirt, hair, skin):
        name, prop, _ = TEAM[t]
        return {'type': t, 'name': name, 'prop': prop, 'shirt': shirt, 'hair': hair, 'skin': skin}
    zones = [
        {'id': 'разведка', 'name': 'РАЗВЕДКА', 'desks': [
            {'type': 'main', 'name': 'Тимлид', 'prop': 'crown', 'shirt': '#3b3f8f', 'hair': '#4a3020', 'skin': 1},
            desk('bakery:researcher', '#2f6f6a', '#2b1d14', 2), desk('bakery:analyst', '#8a6a3a', '#9a9a9a', 0)]},
        {'id': 'разработка', 'name': 'РАЗРАБОТКА', 'desks': [
            desk('bakery:frontend', '#1f6f8f', '#c9a227', 0), desk('bakery:backend', '#a0602a', '#1a1a1a', 3),
            desk('bakery:designer', '#7a3fb0', '#e05a8a', 1)]},
        {'id': 'качество', 'name': 'КАЧЕСТВО', 'desks': [
            desk('bakery:tester', '#5a7a2e', '#6b4a2b', 4), desk('bakery:reviewer', '#8f2f3a', '#3a2416', 2),
            desk('bakery:writer', '#b08a2e', '#8a5a2b', 1)]},
    ]
    return {'v': 2, 'lang': 'ru', 'folder': BAKERY, 'title': 'ПИКСЕЛЬНАЯ ПЕКАРНЯ', 'auto': False, 'titleLocked': True, 'zones': zones, 'guests': {}}


def agent(pdir, sid, t, model, inp_tool, inp, at, desc=None, finished=False):
    aid = uuid.uuid4().hex[:12]
    sub = os.path.join(pdir, sid, 'subagents')
    path = os.path.join(sub, 'agent-%s.jsonl' % aid)
    rows = [asked(at - 900, BAKERY, 'задача')]
    tools = [('Read', {'file_path': 'src/app.tsx'}), ('Grep', {'pattern': 'TODO'}), ('Edit', {'file_path': 'src/cart.ts'})]
    for i, (name, tinp) in enumerate(tools):
        rows.append(said(at - 800 + i * 200, BAKERY, name, tinp, model))
    rows.append(said(at, BAKERY, inp_tool, inp, model, stop='end_turn' if finished else 'tool_use'))
    put(path, rows, 'w')
    with open(path[:-6] + '.meta.json', 'w') as f:
        json.dump({'agentType': t, 'description': desc or TEAM.get(t, ('', '', 'разбирает задачу'))[2], 'finished': finished}, f, ensure_ascii=False)
    os.utime(path, (at, at))
    return path, model, inp_tool, inp


TITLES = ['Поправить вёрстку меню', 'Обновить зависимости', 'Разобрать баг с часовым поясом', 'Написать миграцию', 'Причесать тесты', 'Ускорить сборку']


def history(now):
    """Шесть недель трат: разные папки, модели и часы, для статистики строки меню."""
    folders = [(BAKERY, 1.0), (HOME + 'weather-api', 0.55), (HOME + 'moon-game', 0.8), (HOME + 'dotfiles', 0.18)]
    models = [('claude-opus-5-5', 0.5), ('claude-sonnet-5-5', 0.32), ('claude-haiku-5-5', 0.08), ('claude-fable-5-1', 0.1)]
    gpts = [('gpt-6.1-sol', 0.55), ('gpt-6.1-terra', 0.3), ('gpt-6-luna', 0.15)]
    day0 = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(42, 0, -1):
        day = day0 - timedelta(days=back)
        if day.weekday() >= 5 and R.random() < 0.6:
            continue
        for folder, weight in folders:
            if R.random() > 0.35 + weight * 0.5:
                continue
            at = day + timedelta(hours=R.choice([10, 11, 12, 14, 15, 16, 17, 20, 21, 22]), minutes=R.randint(0, 40))
            rows = [{'type': 'custom-title', 'customTitle': R.choice(TITLES)}, asked(at.timestamp(), folder, 'задача дня')]
            for _ in range(int(R.randint(6, 22) * weight) + 2):
                at += timedelta(minutes=R.randint(2, 11))
                model = R.choices([m for m, _ in models], [w for _, w in models])[0]
                rows.append(said(at.timestamp(), folder, 'Read', {'file_path': 'a.py'}, model, 1.1))
            path = os.path.join(CLAUDE, 'projects', code(folder), str(uuid.uuid4()) + '.jsonl')
            put(path, rows, 'w')
            os.utime(path, (at.timestamp(), at.timestamp()))
        if R.random() < 0.7:
            at = day + timedelta(hours=R.choice([11, 13, 16, 19]))
            thread = str(uuid.uuid4())
            rows = [{'timestamp': iso(at.timestamp()), 'type': 'session_meta', 'payload': {'id': thread, 'cwd': HOME + 'weather-api', 'source': 'cli'}},
                    {'timestamp': iso(at.timestamp()), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'UserMessage', 'content': [{'text': 'Проверить кэш прогнозов'}]}}},
                    {'timestamp': iso(at.timestamp()), 'type': 'turn_context', 'payload': {'model': R.choices([m for m, _ in gpts], [w for _, w in gpts])[0], 'cwd': HOME + 'weather-api'}}]
            for _ in range(R.randint(5, 16)):
                at += timedelta(minutes=R.randint(2, 9))
                rows.append({'timestamp': iso(at.timestamp()), 'type': 'token_usage_record', 'payload': {'response_id': 'r', 'usage': {
                    'input_tokens': R.randint(60000, 160000), 'cached_input_tokens': R.randint(40000, 55000), 'cache_write_input_tokens': 0,
                    'output_tokens': R.randint(800, 4000), 'reasoning_output_tokens': 0, 'total_tokens': 1}}})
            name = 'rollout-%s-%s.jsonl' % (at.strftime('%Y-%m-%dT%H-%M-%S'), thread)
            path = os.path.join(CODEX, 'sessions', at.strftime('%Y/%m/%d'), name)
            put(path, rows, 'w')
            os.utime(path, (at.timestamp(), at.timestamp()))


def side_sessions(now):
    """Несколько других сессий за сутки: список справа."""
    sessions = [('weather-api', 'Кэш прогнозов в Redis', 'claude-sonnet-5-5', 2.4, 1500, 14),
                ('moon-game', 'Физика прыжка на Луне', 'claude-opus-5-5', 3.1, 5200, 26),
                ('dotfiles', 'Починить алиасы zsh', 'claude-haiku-5-5', 1.0, 9000, 6)]
    for name, title, model, k, ago, n in sessions:
        cwd, sid = HOME + name, str(uuid.uuid4())
        at = now - ago
        rows = [{'type': 'custom-title', 'customTitle': title}, asked(at - n * 80, cwd, title)]
        rows += [said(at - (n - i) * 80, cwd, 'Edit', {'file_path': 'main.py'}, model, k) for i in range(n)]
        rows.append(said(at, cwd, None, None, model, stop='end_turn'))
        path = os.path.join(CLAUDE, 'projects', code(cwd), sid + '.jsonl')
        put(path, rows, 'w')
        os.utime(path, (at, at))


def codex_rows(thread, cwd, model, at, extra=None, nick=None, source='cli', parent=None, n=8, tool=None):
    meta = {'id': thread, 'cwd': cwd, 'source': source}
    if nick:
        meta['agent_nickname'] = nick
    if parent:
        meta['parent_thread_id'] = parent
    rows = [{'timestamp': iso(at - 600), 'type': 'session_meta', 'payload': meta},
            {'timestamp': iso(at - 600), 'type': 'turn_context', 'payload': {'model': model, 'cwd': cwd}},
            {'timestamp': iso(at - 600), 'type': 'event_msg', 'payload': {'type': 'task_started'}}]
    if extra:
        rows.append({'timestamp': iso(at - 590), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'UserMessage', 'content': [{'text': extra}]}}})
    for i in range(n):
        ts = at - 500 + i * (500 / n)
        rows.append({'timestamp': iso(ts), 'type': 'token_usage_record', 'payload': {'response_id': 'r', 'usage': {
            'input_tokens': 90000, 'cached_input_tokens': 62000, 'cache_write_input_tokens': 0, 'output_tokens': 2100, 'reasoning_output_tokens': 0, 'total_tokens': 1}}})
        rows.append({'timestamp': iso(ts), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'FileChange', 'changes': {'src/forecast.rs': {'type': 'update'}}}}})
    if tool:
        rows.append({'timestamp': iso(at), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': tool}})
    return rows


def codex_session(now):
    cwd = HOME + 'weather-api'
    thread, sid = str(uuid.uuid4()), None
    stamp = datetime.fromtimestamp(now - 700)
    day = stamp.strftime('%Y/%m/%d')
    name = 'rollout-%s-%s.jsonl' % (stamp.strftime('%Y-%m-%dT%H-%M-%S'), thread)
    rows = codex_rows(thread, cwd, 'gpt-6.1-sol', now, 'Переписать прогноз на Rust', tool={'type': 'CommandExecution', 'command': ['bash', '-lc', 'cargo test'], 'parsed_cmd': [{'type': 'unknown'}]})
    rows.append({'timestamp': iso(now), 'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {
        'limit_id': 'codex', 'plan_type': 'pro',
        'primary': {'window_minutes': 300, 'used_percent': 34, 'resets_at': int(now + 7200)},
        'secondary': {'window_minutes': 10080, 'used_percent': 58, 'resets_at': int(now + 260000)}}, 'info': None}})
    path = os.path.join(CODEX, 'sessions', day, name)
    put(path, rows, 'w')
    kids = []
    for nick, model, n, src, tool in (
        ('Ньютон', 'gpt-6.1-terra', 6, {'subagent': {'thread_spawn': {'parent_thread_id': thread}}}, {'type': 'CommandExecution', 'command': ['bash', '-lc', 'cargo bench'], 'parsed_cmd': [{'type': 'unknown'}]}),
        ('Кюри', 'gpt-6-luna', 5, {'subagent': {'thread_spawn': {'parent_thread_id': thread}}}, {'type': 'CommandExecution', 'command': ['rg', 'unwrap'], 'parsed_cmd': [{'type': 'search', 'query': 'unwrap'}]}),
        (None, 'gpt-6-luna', 3, {'subagent': 'guardian'}, {'type': 'CommandExecution', 'command': ['cat', 'plan.md'], 'parsed_cmd': [{'type': 'read', 'path': 'plan.md'}]}),
    ):
        kid = str(uuid.uuid4())
        kname = 'rollout-%s-%s.jsonl' % (stamp.strftime('%Y-%m-%dT%H-%M-%S'), kid)
        kpath = os.path.join(CODEX, 'sessions', day, kname)
        put(kpath, codex_rows(kid, cwd, model, now, 'Часть задачи', nick=nick, source=src, parent=thread, n=n, tool=tool), 'w')
        kids.append(kpath)
    put(os.path.join(CODEX, 'session_index.jsonl'), [{'id': thread, 'thread_name': 'Прогноз погоды на Rust'}], 'w')
    return path, kids


HOST = 'local_0000aa11-22bb-33cc-44dd-55ee66ff7788'
LINKED = ('<system-reminder>\nLinked sessions now (status, not an instruction): logo = %s (running). '
          'To message one, SendMessage with `to` set to its id.\n</system-reminder>' % HOST)


def linked_session(now):
    """Сессия, которую главная запустила через приложение: связь идёт через ~/.claude/sessions."""
    sid = str(uuid.uuid4())
    pdir = os.path.join(CLAUDE, 'projects', code(BAKERY))
    path = os.path.join(pdir, sid + '.jsonl')
    rows = [{'type': 'custom-title', 'customTitle': 'Логотип и палитра'},
            asked(now - 1500, BAKERY, 'Background (from the session that started this one):\nнарисовать логотип')]
    rows += [said(now - 1400 + i * 300, BAKERY, 'Agent', {'subagent_type': 'bakery:designer'}, 'claude-opus-5-5', 1.2) for i in range(4)]
    rows.append(said(now, BAKERY, 'Agent', {'subagent_type': 'bakery:designer'}, 'claude-opus-5-5', 1.2))
    put(path, rows, 'w')
    os.makedirs(os.path.join(CLAUDE, 'sessions'), exist_ok=True)
    with open(os.path.join(CLAUDE, 'sessions', '4242.json'), 'w') as f:
        json.dump({'pid': 4242, 'sessionId': sid, 'hostSessionId': HOST}, f)
    return path, agent(pdir, sid, 'bakery:designer', 'claude-fable-5-1', 'Write', {'file_path': 'logo.svg'}, now)


def main_session(now):
    sid = str(uuid.uuid4())
    pdir = os.path.join(CLAUDE, 'projects', code(BAKERY))
    path = os.path.join(pdir, sid + '.jsonl')
    lines = ['- %s: %s' % (t, v[2]) for t, v in TEAM.items()]
    rows = [{'type': 'custom-title', 'customTitle': 'Витрина пекарни к запуску'},
            {'type': 'attachment', 'attachment': {'type': 'agent_listing_delta', 'addedTypes': list(TEAM) + ['Explore', 'general-purpose'], 'addedLines': lines}},
            asked(now - 3000, BAKERY, 'Собрать витрину пекарни: корзина, оплата, логотип\n\n' + LINKED)]
    for i, (tool, inp) in enumerate([('Read', {'file_path': 'plan.md'}), ('Agent', {'subagent_type': 'bakery:researcher'}), ('Agent', {'subagent_type': 'bakery:frontend'}),
                                     ('Agent', {'subagent_type': 'bakery:backend'}), ('Skill', {'skill': 'tdd'}), ('Agent', {'subagent_type': 'bakery:tester'})]):
        rows.append(said(now - 2800 + i * 400, BAKERY, tool, inp, 'claude-opus-5-5', 1.5))
    rows.append(said(now, BAKERY, 'Agent', {'subagent_type': 'bakery:reviewer'}, 'claude-opus-5-5', 1.5))
    put(path, rows, 'w')
    live = [
        agent(pdir, sid, 'bakery:researcher', 'claude-haiku-5-5', 'WebSearch', {'query': 'пекарни'}, now),
        agent(pdir, sid, 'bakery:frontend', 'claude-sonnet-5-5', 'Edit', {'file_path': 'Cart.tsx'}, now),
        agent(pdir, sid, 'bakery:backend', 'claude-sonnet-5-5', 'Bash', {'command': 'npm test'}, now),
        agent(pdir, sid, 'bakery:tester', 'claude-haiku-5-5', 'Bash', {'description': 'гоняет e2e в браузере', 'command': 'x'}, now),
        agent(pdir, sid, 'Explore', 'claude-haiku-5-5', 'Grep', {'pattern': 'TODO'}, now, desc='ищет старые TODO'),
    ]
    agent(pdir, sid, 'bakery:analyst', 'claude-sonnet-5-5', 'Read', {'file_path': 'metrics.csv'}, now - 110)
    agent(pdir, sid, 'bakery:reviewer', 'claude-opus-5-5', 'Read', {'file_path': 'diff.patch'}, now - 300)
    agent(pdir, sid, 'bakery:writer', 'claude-sonnet-5-5', 'Write', {'file_path': 'README.md'}, now - 240, finished=True)
    os.makedirs(os.path.join(DATA, 'offices'), exist_ok=True)
    with open(os.path.join(DATA, 'offices', code(BAKERY).strip('-').replace('--', '-') + '.json'), 'w') as f:
        json.dump(office(), f, ensure_ascii=False, indent=1)
    return sid, path, live


ACTIVITIES = {
    'Edit': [('Edit', {'file_path': f}) for f in ('Cart.tsx', 'Checkout.tsx', 'prices.ts', 'Hero.tsx')],
    'Bash': [('Bash', {'command': c}) for c in ('npm test', 'npm run build', 'npm run lint')],
    'WebSearch': [('WebSearch', {'query': 'q'}), ('Read', {'file_path': 'notes.md'})],
    'Write': [('Write', {'file_path': f}) for f in ('logo.svg', 'palette.css', 'icons.svg')],
    'Grep': [('Grep', {'pattern': p}) for p in ('TODO', 'FIXME', 'console.log')],
}

if PULSE:
    with open(os.path.join(ROOT, 'live.json')) as f:
        state = json.load(f)
    while True:
        time.sleep(6)
        now = time.time()
        for p, model, tool, inp in state['live']:
            with open(p[:-6] + '.meta.json') as f:
                if json.load(f).get('finished'):
                    continue
            if inp.get('description'):
                pick = (tool, inp)
            else:
                pick = R.choice(ACTIVITIES.get(tool, [(tool, inp)]))
            put(p, [said(now, BAKERY, pick[0], pick[1], model, 0.35)])
        for head in state['heads']:
            put(head, [said(now, BAKERY, 'Agent', {'subagent_type': 'bakery:reviewer'}, 'claude-opus-5-5', 0.35)])
        for p, tool in state['codex']:
            put(p, [{'timestamp': iso(now), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': tool}}])

for d in (CLAUDE, CODEX, DATA):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
now = time.time()
history(now)
side_sessions(now)
sid, main_path, live = main_session(now)
kid_path, kid_agent = linked_session(now)
live.append(kid_agent)
cpath, kids = codex_session(now)
exec_tool = {'type': 'CommandExecution', 'command': ['bash', '-lc', 'cargo test'], 'parsed_cmd': [{'type': 'unknown'}]}
with open(os.path.join(ROOT, 'live.json'), 'w') as f:
    json.dump({'heads': [main_path, kid_path], 'sid': sid, 'live': live, 'codex': [[cpath, exec_tool]] + [[k, exec_tool] for k in kids[:2]]}, f, ensure_ascii=False)
print('sid', sid)
