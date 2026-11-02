#!/usr/bin/env python3
"""Вымышленный эпик поверх демо agent-office: epic.py <root>, после scripts/demo/gen.py <root>.
По мотивам демо из #13, автор Дмитрий (dmitrykuznetsovdev).
Оркестратор в /home/demo/moon-game, его запустила другая сессия; роли — фоновые сессии в своих worktree."""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

ROOT = os.path.abspath(sys.argv[1])
CLAUDE = os.path.join(ROOT, 'claude')
GAME = '/home/demo/moon-game'
EPIC = 'save-system'
now = time.time()


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def code(folder):
    return ''.join(c if c.isalnum() else '-' for c in folder)


def said(at, cwd, tool, inp, model, stop='tool_use'):
    content = [{'type': 'tool_use', 'name': tool, 'input': inp}] if tool else [{'type': 'text', 'text': 'готово'}]
    return {'type': 'assistant', 'timestamp': iso(at), 'cwd': cwd, 'message': {
        'id': uuid.uuid4().hex, 'model': model, 'content': content, 'stop_reason': stop,
        'usage': {'input_tokens': 200, 'output_tokens': 1400, 'cache_read_input_tokens': 60000, 'cache_creation_input_tokens': 4000}}}


def journal(cwd, sid, title, prompt, end, n, model, busy, bg=True, tool=('Edit', {'file_path': 'src/save.rs'})):
    first = {'type': 'user', 'timestamp': iso(end - n * 90 - 60), 'cwd': cwd, 'message': {'role': 'user', 'content': prompt}}
    if bg:
        first['sessionKind'] = 'bg'
    rows = [{'type': 'custom-title', 'customTitle': title}, first]
    rows += [said(end - (n - i) * 90, cwd, *tool, model) for i in range(n)]
    rows.append(said(end, cwd, *tool, model) if busy else said(end, cwd, None, None, model, stop='end_turn'))
    path = os.path.join(CLAUDE, 'projects', code(cwd), sid + '.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)
    os.utime(path, (end, end))
    return path


def subagent(cwd, sid, kind, desc, end, finished):
    sub = os.path.join(CLAUDE, 'projects', code(cwd), sid, 'subagents')
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, 'agent-%s.jsonl' % uuid.uuid4().hex[:12])
    rows = [{'type': 'user', 'timestamp': iso(end - 600), 'cwd': cwd, 'message': {'role': 'user', 'content': desc}}]
    rows += [said(end - 400 + i * 200, cwd, 'Read', {'file_path': 'reports/S4.md'}, 'claude-haiku-5-5') for i in range(3)]
    with open(path, 'w') as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)
    with open(path[:-6] + '.meta.json', 'w') as f:
        json.dump({'agentType': kind, 'description': desc, 'finished': finished}, f, ensure_ascii=False)
    os.utime(path, (end, end))


WRITER = ('Edit', {'file_path': 'src/save.rs'})
QA = ('Bash', {'command': 'cargo test save'})
# роль, пакет, попытка, состояние, что делает, сколько назад закончила
ROLES = [
    ('critic', 'r1', 1, 'done', 'спека save-system: REJECT, 3 блокера', 5 * 3600),
    ('critic', 'r2', 2, 'done', 'спека save-system: ACCEPT', 4.6 * 3600),
    ('writer', 'S1', 1, 'done', 'S1 готов: формат файла сохранения', 4 * 3600),
    ('qa', 'S1', 1, 'done', 'S1 ACCEPT', 3.8 * 3600),
    ('writer', 'S2', 1, 'done', 'S2 готов: запись на диск атомарно', 3.4 * 3600),
    ('qa', 'S2', 1, 'done', 'S2 ACCEPT', 3.1 * 3600),
    ('writer', 'S3', 1, 'done', 'S3 готов: загрузка и миграция версий', 2.6 * 3600),
    ('qa', 'S3', 1, 'done', 'S3 ACCEPT', 2.2 * 3600),
    ('writer', 'S4', 1, 'done', 'S4 готов: слоты сохранений в меню', 1.4 * 3600),
    ('qa', 'S4', 1, 'working', 'гоняет тесты слотов на трёх платформах', 20),
    ('writer', 'S5', 1, 'done', 'S5 поп. 1: автосейв по таймеру', 1.9 * 3600),
    ('qa', 'S5', 1, 'done', 'S5 REJECT: гонка автосейва с ручным сохранением', 1.6 * 3600),
    ('writer', 'S5', 2, 'working', 'чинит гонку автосейва, пишет тест на неё', 12),
]
NAMES = {'writer': 'писатель', 'qa': 'QA', 'critic': 'критик'}
seats = {}  # (пакет, роль) → роль последней попытки
sessions, worktrees = [], []
for role, task, attempt, state, detail, ago in ROLES:
    sid = str(uuid.uuid4())
    tree = '%s/.claude/worktrees/%s-%s-%s%s' % (GAME, EPIC, task.lower(), role, '' if attempt == 1 else '-%d' % attempt)
    busy = state == 'working'
    journal(tree, sid, '%s · %s · %s' % (EPIC, task, NAMES[role]), 'Роль %s пакета %s эпика %s. Прочитай бриф.' % (role, task, EPIC),
            now - ago, 14 if busy else 9, 'claude-opus-5-5' if role != 'writer' else 'claude-sonnet-5-5', busy, tool=QA if role == 'qa' else WRITER)
    sessions.append({'session': sid, 'role': role, 'task': task})
    worktrees.append({'path': tree, 'role': role, 'task': task})
    seats[(task, role)] = {'role': role, 'session': sid, 'state': state, 'detail': detail}

coord = str(uuid.uuid4())
journal(GAME, coord, 'Эпик save-system: сохранения игры',
        'Background (from the session that started this one):\nвести эпик save-system через orchestrating-epics',
        now - 8, 40, 'claude-opus-5-5', True, bg=False, tool=('Bash', {'command': 'git log --oneline epic/save-system'}))
subagent(GAME, coord, 'general-purpose', 'сверяет отчёт S4 с контрактом', now - 15, False)
subagent(GAME, coord, 'Explore', 'ищет, кто ещё пишет в save.rs', now - 900, True)

landed = ['passed'] * 3


def package(pid, attempt, stage, gates, text, is_open, roles, waits=()):
    return {'id': pid, 'attempt': attempt, 'stage': stage, 'gates': gates, 'text': text, 'is_open': is_open,
            'roles': [seats[(pid, r)] for r in roles], 'waits_for': list(waits)}


view = {
    'schema': 1, 'updated_at': iso(now), 'epic': EPIC, 'revision': 2, 'status': None,
    'coordinator': {'session': coord, 'task_id': 'local_demo', 'context_tokens': 118000, 'context_limit': 250000, 'owner_needed': False},
    'progress': {'landed': 3, 'total': 7},
    'packages': [
        package('S1', 1, 'INTEGRATED', landed, 'поп. 1 · ACCEPT', False, ['writer', 'qa']),
        package('S2', 1, 'INTEGRATED', landed, 'поп. 1 · ACCEPT', False, ['writer', 'qa']),
        package('S3', 1, 'INTEGRATED', landed, 'поп. 1 · ACCEPT', False, ['writer', 'qa']),
        package('S4', 1, 'PATCH_READY', ['passed', 'next', 'ahead'], 'поп. 1 · QA проверяет', True, ['writer', 'qa']),
        package('S5', 2, 'IMPLEMENTING', ['next', 'ahead', 'ahead'], 'поп. 2 после REJECT', True, ['writer']),
        package('S6', 0, 'PLANNED', ['ahead'] * 3, 'не выдан', False, [], ['S4']),
        package('S7', 0, 'PLANNED', ['ahead'] * 3, 'не выдан', False, []),
    ],
    'critiques': [{'round': 1, 'verdict': 'REJECT', 'roles': [seats[('r1', 'critic')]]},
                  {'round': 2, 'verdict': 'ACCEPT', 'roles': [seats[('r2', 'critic')]]}],
    'researchers': [],
    'sessions': sessions,
    'worktrees': worktrees,
    'release': {'from': 'refs/heads/epic/%s' % EPIC, 'to': 'refs/heads/main', 'state': 'packages'},
}
out = os.path.join(CLAUDE, 'epics', EPIC, 'view.json')
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w') as f:
    json.dump(view, f, ensure_ascii=False, indent=1)
print('coord', coord)
