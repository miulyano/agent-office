import datetime
import http.client
import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import server  # noqa: E402


def line(**entry):
    return json.dumps(entry, ensure_ascii=False) + '\n'


def local_day(offset):
    """Локальная полночь дня offset (0 — сегодня, -1 — вчера), секунды."""
    d = datetime.date.today() + datetime.timedelta(days=offset)
    return time.mktime(d.timetuple())


def iso(seconds):
    return datetime.datetime.utcfromtimestamp(seconds).strftime('%Y-%m-%dT%H:%M:%SZ')


def assistant(mid, tools=(), stop='tool_use', usage=None, ts='2026-01-01T10:00:00Z'):
    content = [{'type': 'tool_use', 'name': name, 'input': inp} for name, inp in tools]
    return line(type='assistant', timestamp=ts, cwd='/work/app', message={
        'id': mid, 'model': 'claude-sonnet-5-5', 'stop_reason': stop, 'content': content,
        'usage': usage or {'input_tokens': 1000, 'output_tokens': 100},
    })


class DescribeTest(unittest.TestCase):
    def test_files_and_commands_read_as_actions(self):
        self.assertEqual(server.describe('Read', {'file_path': '/a/b/cuts.json'}), 'читает cuts.json')
        self.assertEqual(server.describe('Bash', {'command': 'cd /x && python3 tools/run.py build v2'}), '$ run.py build')
        self.assertEqual(server.describe('Bash', {'command': 'ls', 'description': 'Список файлов'}), 'Список файлов')
        self.assertEqual(server.describe('Bash', {'command': 'python3 - <<EOF'}), 'считает скриптом')
        self.assertEqual(server.describe('Bash', {'command': 'until [ -f a ]; do sleep 1; done'}), 'ждёт')

    def test_unknown_mcp_tool_shows_its_name(self):
        self.assertEqual(server.describe('mcp__srv__do_thing', {}), 'srv do_thing')


class MoneyFormatTest(unittest.TestCase):
    """Суммы в офисе и в окошке строки меню: один знак после запятой."""

    def money(self, *values):
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('нет node')
        web = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web', 'office.js')
        script = (
            f"import {{ money }} from {json.dumps('file://' + os.path.abspath(web))};"
            f"console.log(JSON.stringify({json.dumps(values)}.map(money)))"
        )
        out = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_one_decimal_at_any_size(self):
        self.assertEqual(self.money(0, 0.96, 1.26, 12.04, 123.45), ['$0.0', '$1.0', '$1.3', '$12.0', '$123.5'])

    def test_thousands_are_grouped(self):
        self.assertEqual(self.money(1234.56), ['$1\u00a0234.6'])

    def test_a_cent_is_not_shown_as_nothing(self):
        self.assertEqual(self.money(0.04), ['<$0.1'])

    def test_popover_uses_the_shared_format(self):
        web = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web', 'menubar.js')
        with open(web, encoding='utf-8') as f:
            source = f.read()
        self.assertIn("from './office.js'", source)
        self.assertNotIn('function money', source)


class CostTest(unittest.TestCase):
    def test_price_per_million_with_cache(self):
        usage = {'input_tokens': 1_000_000, 'output_tokens': 1_000_000, 'cache_read_input_tokens': 1_000_000}
        self.assertAlmostEqual(server.message_cost('claude-sonnet-5-5', usage), 2.0 + 10.0 + 0.2)

    def test_unknown_model_is_free(self):
        self.assertEqual(server.message_cost('other-model', {'input_tokens': 10}), 0.0)


class TailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 's.jsonl')

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text, mode='w'):
        with open(self.path, mode) as f:
            f.write(text)

    def test_roster_title_cost_and_activity(self):
        self.write(
            line(type='custom-title', customTitle='Ремонт сборки')
            + line(type='attachment', attachment={
                'type': 'agent_listing_delta',
                'addedTypes': ['Explore', 'kit:reviewer'],
                'addedLines': ['- Explore: Search agent', '- kit:reviewer: Reviews diffs'],
            })
            + line(type='user', timestamp='2026-01-01T10:00:00Z', cwd='/work/app/.claude/worktrees/x',
                   message={'role': 'user', 'content': 'почини сборку'})
            # a reply logged in two pieces: paid once
            + assistant('m1', [('Read', {'file_path': '/work/app/Makefile'})])
            + assistant('m1', [('Grep', {'pattern': 'build'})])
        )
        t = server.Tail(self.path).feed()
        self.assertEqual(t.title, 'Ремонт сборки')
        self.assertEqual(t.home, '/work/app')
        self.assertEqual(t.roster, {'Explore': 'Search agent', 'kit:reviewer': 'Reviews diffs'})
        self.assertEqual(t.calls, 2)
        self.assertEqual(t.activity, 'ищет build')
        self.assertTrue(t.busy)
        self.assertAlmostEqual(t.cost, server.message_cost('claude-sonnet-5-5', {'input_tokens': 1000, 'output_tokens': 100}))

    def test_reads_only_what_was_appended(self):
        self.write(assistant('m1', [('Read', {'file_path': 'a'})]))
        t = server.Tail(self.path).feed()
        self.write(assistant('m2', stop='end_turn'), mode='a')
        t.feed()
        self.assertEqual(t.calls, 1)
        self.assertFalse(t.busy)

    def test_cost_is_split_by_local_day(self):
        self.write(assistant('m1', ts=iso(local_day(-1) + 3600)) + assistant('m2', ts=iso(local_day(0) + 3600)))
        t = server.Tail(self.path).feed()
        one = server.message_cost('claude-sonnet-5-5', {'input_tokens': 1000, 'output_tokens': 100})
        self.assertAlmostEqual(t.daily[server.day_of(local_day(0) * 1000)], one)
        self.assertAlmostEqual(server.today_cost(t), one)
        self.assertAlmostEqual(t.cost, 2 * one)

    def test_model_and_tool_kind_follow_the_last_reply(self):
        self.write(assistant('m1', [('Edit', {'file_path': '/a/cuts.json'})]))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.model, 'claude-sonnet-5-5')
        self.assertEqual(t.tool, 'edit')
        self.assertEqual(t.activity, 'правит cuts.json')
        self.write(line(type='assistant', timestamp='2026-01-01T10:01:00Z', message={
            'id': 'm2', 'model': 'claude-haiku-5-5', 'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'npm test'}}],
            'usage': {'input_tokens': 1, 'output_tokens': 1}}), mode='a')
        t.feed()
        self.assertEqual(t.model, 'claude-haiku-5-5')
        self.assertEqual(t.tool, 'bash')

    def test_tool_kinds(self):
        kinds = {name: server.tool_kind(name) for name in
                 ['Read', 'Write', 'Edit', 'MultiEdit', 'Grep', 'Glob', 'Bash', 'WebFetch', 'Agent', 'Task', 'Skill', 'mcp__x__y']}
        self.assertEqual(kinds, {'Read': 'read', 'Write': 'write', 'Edit': 'edit', 'MultiEdit': 'edit', 'Grep': 'grep',
                                 'Glob': 'grep', 'Bash': 'bash', 'WebFetch': 'web', 'Agent': 'agent', 'Task': 'agent',
                                 'Skill': 'skill', 'mcp__x__y': 'other'})

    def test_removed_agents_leave_the_roster(self):
        self.write(line(type='attachment', attachment={'type': 'agent_listing_delta', 'addedTypes': ['a', 'b']})
                   + line(type='attachment', attachment={'type': 'agent_listing_delta', 'removedTypes': ['a']}))
        self.assertEqual(list(server.Tail(self.path).feed().roster), ['b'])

    def test_work_is_the_turns_not_the_pause_between_them(self):
        def user(ts, content, **more):
            return line(type='user', timestamp=ts, message={'role': 'user', 'content': content}, **more)
        self.write(
            user('2026-01-01T10:00:00Z', 'почини сборку')
            + assistant('m1', [('Bash', {'command': 'make'})], ts='2026-01-01T10:01:00Z')
            + user('2026-01-01T10:03:00Z', [{'type': 'tool_result', 'content': 'ok'}])
            + assistant('m2', stop='end_turn', ts='2026-01-01T10:04:00Z')
            # after the turn the journal may gain a note: nobody is working
            + user('2026-01-01T11:00:00Z', 'напоминание системы', isMeta=True)
            + user('2026-01-01T12:00:00Z', 'дальше')
            + assistant('m3', [('Read', {'file_path': 'a'})], ts='2026-01-01T12:02:00Z')
        )
        t = server.Tail(self.path).feed()
        self.assertEqual(t.worked(0, False), [
            [server.ms('2026-01-01T10:00:00Z'), server.ms('2026-01-01T10:04:00Z')],
            [server.ms('2026-01-01T12:00:00Z'), server.ms('2026-01-01T12:02:00Z')]])

    def test_interrupted_turn_ends_at_its_last_record(self):
        def user(ts, content):
            return line(type='user', timestamp=ts, message={'role': 'user', 'content': content})
        self.write(user('2026-01-01T10:00:00Z', 'почини')
                   + assistant('m1', [('Read', {'file_path': 'a'})], ts='2026-01-01T10:01:00Z')
                   + user('2026-01-01T12:00:00Z', 'нет, другое'))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.worked(0, False), [
            [server.ms('2026-01-01T10:00:00Z'), server.ms('2026-01-01T10:01:00Z')],
            [server.ms('2026-01-01T12:00:00Z'), server.ms('2026-01-01T12:00:00Z')]])

    def test_turn_in_progress_runs_until_now_only_while_it_is_open(self):
        def user(ts, content):
            return line(type='user', timestamp=ts, message={'role': 'user', 'content': content})
        self.write(user('2026-01-01T10:00:00Z', 'почини') + assistant('m1', [('Read', {'file_path': 'a'})], ts='2026-01-01T10:01:00Z'))
        t = server.Tail(self.path).feed()
        now = server.ms('2026-01-01T10:01:30Z')
        self.assertEqual(t.worked(now, True), [[server.ms('2026-01-01T10:00:00Z'), now]])
        self.write(assistant('m2', stop='end_turn', ts='2026-01-01T10:02:00Z'), mode='a')
        self.assertEqual(t.feed().worked(now, True), [[server.ms('2026-01-01T10:00:00Z'), server.ms('2026-01-01T10:02:00Z')]])

    def test_agent_called_again_does_not_count_the_pause(self):
        def user(ts, content, **more):
            return line(type='user', timestamp=ts, message={'role': 'user', 'content': content}, **more)
        # an agent's journal has no end_turn, and the second call arrives as a meta record
        self.write(user('2026-01-01T10:00:00Z', 'ищи')
                   + assistant('m1', [('Grep', {'pattern': 'x'})], ts='2026-01-01T10:01:00Z')
                   + user('2026-01-01T10:02:00Z', [{'type': 'tool_result', 'content': 'ok'}])
                   + user('2026-01-01T18:00:00Z', 'поищи ещё', isMeta=True)
                   + assistant('m2', [('Grep', {'pattern': 'y'})], ts='2026-01-01T18:01:00Z')
                   + user('2026-01-01T18:02:00Z', [{'type': 'tool_result', 'content': 'ok'}]))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.worked(0, False), [
            [server.ms('2026-01-01T10:00:00Z'), server.ms('2026-01-01T10:02:00Z')],
            [server.ms('2026-01-01T18:00:00Z'), server.ms('2026-01-01T18:02:00Z')]])

    def test_journal_without_a_prompt_still_counts_its_work(self):
        self.write(assistant('m1', [('Read', {'file_path': 'a'})], ts='2026-01-01T10:00:00Z')
                   + assistant('m2', stop='end_turn', ts='2026-01-01T10:01:00Z'))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.worked(0, False), [[server.ms('2026-01-01T10:00:00Z'), server.ms('2026-01-01T10:01:00Z')]])

    def test_overlapping_work_counts_once(self):
        spans = [[10, 110], [60, 160], [310, 410], [320, 330]]
        self.assertEqual(server.work_clock(spans, 1000), {'worked': 250, 'workingSince': 0})
        self.assertEqual(server.work_clock([], 1000), {'worked': 0, 'workingSince': 0})

    def test_running_stretch_is_reported_by_its_start(self):
        self.assertEqual(server.work_clock([[10, 110], [300, 500], [400, 1000]], 1000), {'worked': 100, 'workingSince': 300})

    def test_meta_record_during_a_tool_call_keeps_the_turn(self):
        def tool(mid, ts):
            return line(type='assistant', timestamp=ts, cwd='/work/app', message={
                'id': mid, 'model': 'claude-sonnet-5-5', 'stop_reason': 'tool_use',
                'content': [{'type': 'tool_use', 'id': 'c1', 'name': 'Bash', 'input': {'command': 'make'}}],
                'usage': {'input_tokens': 1, 'output_tokens': 1}})
        self.write(line(type='user', timestamp='2026-01-01T10:00:00Z', message={'role': 'user', 'content': 'собери'})
                   + tool('m1', '2026-01-01T10:00:10Z')
                   + line(type='user', timestamp='2026-01-01T10:04:00Z', isMeta=True, message={'role': 'user', 'content': 'заметка'})
                   + line(type='user', timestamp='2026-01-01T10:05:00Z', message={
                       'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c1', 'content': 'ok'}]}))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.worked(0, False), [[server.ms('2026-01-01T10:00:00Z'), server.ms('2026-01-01T10:05:00Z')]])

    def test_interrupt_ends_the_turn_even_without_a_tool_result(self):
        ts = '2026-01-01T10:00:00Z'
        self.write(line(type='user', timestamp=ts, message={'role': 'user', 'content': 'собери'})
                   + line(type='assistant', timestamp='2026-01-01T10:01:00Z', message={'id': 'm1', 'stop_reason': 'tool_use', 'content': [
                       {'type': 'tool_use', 'id': 'c1', 'name': 'Bash', 'input': {'command': 'make'}}]})
                   + line(type='user', timestamp='2026-01-01T10:02:00Z', message={'role': 'user', 'content': [
                       {'type': 'text', 'text': '[Request interrupted by user for tool use]'}]}))
        t = server.Tail(self.path).feed()
        now = server.ms(ts) + (server.LIVE_S + 300) * 1000
        self.assertEqual(t.worked(now), [[server.ms(ts), server.ms('2026-01-01T10:01:00Z')]])

    def test_long_tool_call_keeps_running_past_the_quiet_limit(self):
        ts = '2026-01-01T10:00:00Z'
        self.write(line(type='user', timestamp=ts, message={'role': 'user', 'content': 'собери'})
                   + line(type='assistant', timestamp=ts, message={'id': 'm1', 'stop_reason': 'tool_use', 'content': [
                       {'type': 'tool_use', 'id': 'c1', 'name': 'Agent', 'input': {}}]}))
        t = server.Tail(self.path).feed()
        now = server.ms(ts) + (server.LIVE_S + 300) * 1000
        self.assertEqual(t.worked(now), [[server.ms(ts), now]])

    def test_clock_never_goes_back_when_a_turn_falls_silent(self):
        ts = '2026-01-01T10:00:00Z'
        self.write(line(type='user', timestamp=ts, message={'role': 'user', 'content': 'почини'}))
        t = server.Tail(self.path).feed()
        seen = server.ms(ts) + (server.LIVE_S - 1) * 1000
        self.assertEqual(t.worked(seen), [[server.ms(ts), seen]])
        self.assertEqual(t.worked(seen + 3600000), [[server.ms(ts), seen]])

    def test_never_seen_turn_stops_at_its_last_record(self):
        ts = '2026-01-01T10:00:00Z'
        self.write(line(type='user', timestamp=ts, message={'role': 'user', 'content': 'почини'}))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.worked(server.ms(ts) + (server.LIVE_S + 1) * 1000), [[server.ms(ts), server.ms(ts)]])

    def test_records_without_work_do_not_open_a_turn(self):
        def user(ts, content):
            return line(type='user', timestamp=ts, message={'role': 'user', 'content': content})
        ts = '2026-01-01T10:00:%02dZ'
        for n, note in enumerate(('<local-command-stdout>Set model</local-command-stdout>', '<command-name>/model</command-name>',
                                  [{'type': 'text', 'text': '[Request interrupted by user]'}])):
            self.write(user(ts % 0, 'почини') + assistant('m1', stop='end_turn', ts=ts % 1) + user(ts % 2, note))
            t = server.Tail(self.path).feed()
            self.assertEqual(t.worked(server.ms(ts % 30)), [[server.ms(ts % 0), server.ms(ts % 1)]], note)

    def test_stale_note_does_not_stretch_later_work(self):
        def user(ts, content, **more):
            return line(type='user', timestamp=ts, message={'role': 'user', 'content': content}, **more)
        self.write(user('2026-01-01T10:00:00Z', 'почини') + assistant('m1', stop='end_turn', ts='2026-01-01T10:01:00Z')
                   + user('2026-01-01T10:05:00Z', 'заметка', isMeta=True)
                   + assistant('m2', stop='end_turn', ts='2026-01-01T18:00:00Z'))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.worked(0, False)[-1], [server.ms('2026-01-01T18:00:00Z')] * 2)

    def test_record_without_time_does_not_break_the_journal(self):
        untimed = line(type='assistant', message={'id': 'm0', 'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': 'Read', 'input': {}}]})
        self.write(untimed + assistant('m1', [('Read', {'file_path': 'a'})], ts='2026-01-01T10:00:00Z')
                   + line(type='user', timestamp='2026-01-01T10:01:00Z', message={'role': 'user', 'content': [{'type': 'tool_result', 'content': 'ok'}]}))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.worked(0, False), [[server.ms('2026-01-01T10:00:00Z'), server.ms('2026-01-01T10:01:00Z')]])

    def test_command_without_time_is_not_counted_from_the_epoch(self):
        t = server.Tail(self.path)
        t.shells['b1'] = {'id': 'b1', 'name': 'x', 'status': 'completed', 'start': 0, 'end': server.ms('2026-01-01T10:00:00Z')}
        self.assertEqual(t.worked(0, False), [])

    def test_turn_starts_at_the_last_prompt(self):
        def user(ts, content, **more):
            return line(type='user', timestamp=ts, message={'role': 'user', 'content': content}, **more)
        self.write(
            user('2026-01-01T10:00:00Z', 'почини сборку')
            + assistant('m1', stop='end_turn', ts='2026-01-01T10:01:00Z')
            # a picture with a caption comes as a list of blocks, not a string
            + user('2026-01-02T09:00:00Z', [{'type': 'image'}, {'type': 'text', 'text': 'что это?'}])
            + assistant('m2', [('Read', {'file_path': 'a'})], ts='2026-01-02T09:00:10Z')
            # none of these is a new request
            + user('2026-01-02T09:00:20Z', [{'type': 'tool_result', 'content': 'ok'}])
            + user('2026-01-02T09:00:30Z', 'напоминание системы', isMeta=True)
            + user('2026-01-02T09:00:40Z', 'краткое содержание', isCompactSummary=True)
        )
        t = server.Tail(self.path).feed()
        self.assertEqual(t.turn, server.ms('2026-01-02T09:00:00Z'))
        self.assertEqual(t.first, server.ms('2026-01-01T10:00:00Z'))


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.projects, server.PROJECTS = server.PROJECTS, self.dir.name
        self.codex, server.CODEX_SESSIONS = server.CODEX_SESSIONS, os.path.join(self.dir.name, 'нет-codex')
        server.TAILS.clear()

    def tearDown(self):
        server.PROJECTS = self.projects
        server.CODEX_SESSIONS = self.codex
        server.TAILS.clear()
        self.dir.cleanup()

    def test_sessions_report_today(self):
        pdir = os.path.join(self.dir.name, '-work-app')
        sub = os.path.join(pdir, 'sid1', 'subagents')
        os.makedirs(sub)
        yesterday, today = iso(local_day(-1) + 3600), iso(local_day(0) + 3600)
        with open(os.path.join(pdir, 'sid1.jsonl'), 'w') as f:
            f.write(assistant('m1', ts=yesterday) + assistant('m2', ts=today))
        with open(os.path.join(sub, 'agent-a1.jsonl'), 'w') as f:
            f.write(assistant('a1', ts=yesterday) + assistant('a2', ts=today))
        with open(os.path.join(sub, 'agent-a1.meta.json'), 'w') as f:
            json.dump({'agentType': 'Explore', 'description': 'ищет', 'finished': True}, f)
        one = server.message_cost('claude-sonnet-5-5', {'input_tokens': 1000, 'output_tokens': 100})
        [s] = server.sessions()
        self.assertEqual(s['id'], 'sid1')
        self.assertAlmostEqual(s['today'], round(2 * one, 2))
        self.assertAlmostEqual(s['cost'], round(4 * one, 2))


    def test_agents_and_producer_carry_model_tool_and_finish(self):
        pdir = os.path.join(self.dir.name, '-work-app')
        sub = os.path.join(pdir, 'sid1', 'subagents')
        os.makedirs(sub)
        with open(os.path.join(pdir, 'sid1.jsonl'), 'w') as f:
            f.write(assistant('m1', [('Agent', {'subagent_type': 'Explore'})]))
        with open(os.path.join(sub, 'agent-a1.jsonl'), 'w') as f:
            f.write(line(type='assistant', timestamp='2026-01-01T10:00:05Z', message={
                'id': 'a1', 'model': 'claude-haiku-5-5', 'stop_reason': 'tool_use',
                'content': [{'type': 'tool_use', 'name': 'Grep', 'input': {'pattern': 'TODO'}}],
                'usage': {'input_tokens': 1, 'output_tokens': 1}}))
        with open(os.path.join(sub, 'agent-a1.meta.json'), 'w') as f:
            json.dump({'agentType': 'Explore', 'description': 'ищет', 'finished': True}, f)
        st = server.state('sid1')
        [a] = st['agents']
        self.assertEqual((a['model'], a['tool'], a['finished']), ('claude-haiku-5-5', 'grep', True))
        self.assertEqual(a['at'], server.ms('2026-01-01T10:00:05Z'))
        self.assertEqual((st['producer']['model'], st['producer']['tool']), ('claude-sonnet-5-5', 'agent'))

    def test_clock_counts_from_the_current_turn(self):
        pdir = os.path.join(self.dir.name, '-work-app')
        os.makedirs(pdir)
        with open(os.path.join(pdir, 'sid1.jsonl'), 'w') as f:
            f.write(assistant('m0', stop='end_turn', ts='2026-01-01T10:00:00Z')
                    + line(type='user', timestamp='2026-01-02T09:00:00Z', message={'role': 'user', 'content': 'дальше'})
                    + assistant('m1', [('Read', {'file_path': 'a'})], ts='2026-01-02T09:00:10Z'))
        st = server.state('sid1')
        self.assertEqual(st['since'], server.ms('2026-01-02T09:00:00Z'))
        self.assertEqual(st['sessions'][0]['since'], server.ms('2026-01-02T09:00:00Z'))

    def test_clock_without_a_prompt_counts_from_the_first_entry(self):
        pdir = os.path.join(self.dir.name, '-work-app')
        os.makedirs(pdir)
        with open(os.path.join(pdir, 'sid1.jsonl'), 'w') as f:
            f.write(assistant('m1', [('Read', {'file_path': 'a'})], ts='2026-01-01T10:00:00Z'))
        self.assertEqual(server.state('sid1')['since'], server.ms('2026-01-01T10:00:00Z'))


class LedgerTest(unittest.TestCase):
    one = {'input_tokens': 1000, 'output_tokens': 100}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.pdir = os.path.join(self.dir.name, '-work-app')
        os.makedirs(os.path.join(self.pdir, 'sid1', 'subagents'))
        self.main = os.path.join(self.pdir, 'sid1.jsonl')
        self.agent = os.path.join(self.pdir, 'sid1', 'subagents', 'agent-a1.jsonl')
        self.cache = os.path.join(self.dir.name, 'stats.json')
        self.usd = server.message_cost('claude-sonnet-5-5', self.one)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, path, text, mode='w'):
        with open(path, mode) as f:
            f.write(text)

    def day(self, offset):
        return server.day_of(local_day(offset) * 1000)

    def test_parts_add_up_to_message_cost(self):
        usage = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 30,
                 'cache_creation': {'ephemeral_5m_input_tokens': 40, 'ephemeral_1h_input_tokens': 50}, 'speed': 'fast'}
        self.assertAlmostEqual(sum(server.cost_parts('claude-opus-5-5', usage)), server.message_cost('claude-opus-5-5', usage))

    def test_days_split_by_model_project_and_session(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(-1) + 3600)) + assistant('m1', ts=iso(local_day(-1) + 3600))
                   + assistant('m2', ts=iso(local_day(0) + 3600)))
        self.write(self.agent, assistant('a1', ts=iso(local_day(0) + 7200)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        days = {d['day']: d for d in ledger.report()['days']}
        self.assertAlmostEqual(days[self.day(-1)]['usd'], self.usd)  # m1 logged twice, paid once
        today = days[self.day(0)]
        self.assertAlmostEqual(today['usd'], 2 * self.usd)
        self.assertAlmostEqual(today['models']['claude-sonnet-5-5'], 2 * self.usd)
        self.assertAlmostEqual(today['projects']['app'], 2 * self.usd)
        self.assertEqual(today['sessions'], 1)  # subagent is not a session of its own
        self.assertEqual((days[self.day(-1)]['started'], today['started']), (1, 0))  # started yesterday
        self.assertEqual(today['tokens'], 2200)

    def test_tokens_split_by_kind_model_and_project(self):
        usage = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 30,
                 'cache_creation': {'ephemeral_5m_input_tokens': 40, 'ephemeral_1h_input_tokens': 50}}
        self.write(self.main, assistant('m1', usage=usage, ts=iso(local_day(0) + 3600)))
        self.write(self.agent, assistant('a1', ts=iso(local_day(0) + 7200)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        today = ledger.report()['days'][-1]
        self.assertEqual(today['tokParts'], [1010, 120, 30, 90])  # input, output, cache read, cache write
        self.assertEqual(today['tokModels'], {'claude-sonnet-5-5': 1250})
        self.assertEqual(today['tokProjects'], {'app': 1250})
        self.assertEqual(today['tokens'], 1250)

    def old_cache(self, journal_offset):
        """stats.json before tokens were split: cells end with the total token count."""
        hour = time.strftime('%Y-%m-%dT%H', time.localtime(local_day(-1) + 3600))
        self.write(self.cache, json.dumps({
            'files': {self.main: {'offset': journal_offset, 'ids': ['m1'], 'days': [hour[:10]], 'main': True}},
            'hours': {hour: {'app': {'claude-sonnet-5-5': [self.usd, 0.0, 0.0, 0.0, 1100]}}},
        }))

    def test_old_cache_is_rebuilt_while_its_journals_are_on_disk(self):
        text = assistant('m1', ts=iso(local_day(-1) + 3600))
        self.write(self.main, text)
        self.old_cache(len(text.encode()))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        day = ledger.report()['days'][0]
        self.assertAlmostEqual(day['usd'], self.usd)  # read again, not counted twice
        self.assertEqual(day['tokParts'], [1000, 100, 0, 0])

    def test_old_cache_keeps_totals_when_journals_are_gone(self):
        self.old_cache(500)
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        day = ledger.report()['days'][0]
        self.assertAlmostEqual(day['usd'], self.usd)
        self.assertEqual((day['tokens'], day['tokParts']), (1100, [0, 0, 0, 0]))  # no split for that history
        # a new journal writing into the same hour adds to the old cell
        self.write(os.path.join(self.pdir, 'sid2.jsonl'), assistant('m2', ts=iso(local_day(-1) + 3600)))
        ledger.scan(self.dir.name)
        day = ledger.report()['days'][0]
        self.assertEqual((day['tokens'], day['tokParts']), (2200, [1000, 100, 0, 0]))

    def test_report_fills_quiet_days_and_rolls_24h(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(-3) + 3600)) + assistant('m2', ts=iso(time.time() - 600)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        report = ledger.report()
        self.assertEqual(report['since'], self.day(-3))
        self.assertEqual([d['day'] for d in report['days']], [self.day(i) for i in (-3, -2, -1, 0)])
        self.assertEqual(report['days'][1]['usd'], 0)
        self.assertAlmostEqual(report['last24'], self.usd)

    def test_reads_only_appended_lines(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(0) + 3600)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        self.write(self.main, assistant('m2', ts=iso(local_day(0) + 3600)), mode='a')
        ledger.scan(self.dir.name)
        ledger.scan(self.dir.name)
        self.assertAlmostEqual(ledger.report()['days'][-1]['usd'], 2 * self.usd)

    def test_history_outlives_deleted_journals(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(-1) + 3600)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        ledger.save()
        os.remove(self.main)
        again = server.Ledger(self.cache)
        again.scan(self.dir.name)
        self.assertAlmostEqual(again.report()['days'][0]['usd'], self.usd)

    def test_reloaded_cache_does_not_count_twice(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(0) + 3600)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        ledger.save()
        again = server.Ledger(self.cache)
        again.scan(self.dir.name)
        self.assertAlmostEqual(again.report()['days'][-1]['usd'], self.usd)

    def test_hours_for_heatmap(self):
        at = local_day(0) + 14 * 3600 + 60
        self.write(self.main, assistant('m1', ts=iso(at)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        [[day, hour, usd]] = ledger.report()['hours']
        self.assertEqual((day, hour), (self.day(0), 14))
        self.assertAlmostEqual(usd, self.usd)


THREAD = '01a18b44-21b2-7481-961a-0f2ee2ca8ea2'


def rec(kind, payload, ts='2026-01-01T10:00:00.000Z'):
    return line(type=kind, timestamp=ts, payload=payload)


def meta(thread=THREAD, cwd='/work/app', source='vscode', **more):
    return rec('session_meta', dict(id=thread, session_id=thread, cwd=cwd, source=source, originator='Codex Desktop', **more))


def context(model='gpt-5.6-sol', cwd='/work/app', ts='2026-01-01T10:00:00.000Z'):
    return rec('turn_context', {'model': model, 'cwd': cwd}, ts)


def event(kind, ts='2026-01-01T10:00:00.000Z', **more):
    return rec('event_msg', dict(type=kind, **more), ts)


def usage_record(total_in=1000, cached=800, out=100, ts='2026-01-01T10:00:01.000Z'):
    return rec('token_usage_record', {'response_id': 'r', 'usage': {
        'input_tokens': total_in, 'cached_input_tokens': cached, 'cache_write_input_tokens': 0,
        'output_tokens': out, 'reasoning_output_tokens': 0, 'total_tokens': total_in + out}}, ts)


def token_count(total_in, cached, out, ts='2026-01-01T10:00:02.000Z', limits=None):
    return event('token_count', ts, rate_limits=limits, info={'total_token_usage': {
        'input_tokens': total_in, 'cached_input_tokens': cached, 'output_tokens': out, 'total_tokens': total_in + out}})


def item(kind, ts='2026-01-01T10:00:03.000Z', **more):
    return event('item_completed', ts, item=dict(type=kind, **more))


USAGE = {'input_tokens': 200, 'output_tokens': 100, 'cache_read_input_tokens': 800, 'cache_creation_input_tokens': 0}


class CodexUsageTest(unittest.TestCase):
    def fold(self, text):
        meter, out = {}, []
        for raw in text.splitlines():
            u = server.codex_usage(meter, json.loads(raw))
            if u:
                out.append(u)
        return out

    def test_cached_tokens_leave_the_input(self):
        self.assertEqual(self.fold(context() + usage_record()), [USAGE])

    def test_price_is_per_million_of_uncached_input(self):
        usd = server.message_cost('gpt-5.6-sol', {'input_tokens': 1_000_000, 'output_tokens': 1_000_000, 'cache_read_input_tokens': 1_000_000})
        self.assertAlmostEqual(usd, 4.0 + 20.0 + 0.4)

    def test_old_logs_count_by_growth_of_the_running_total(self):
        got = self.fold(context() + token_count(1000, 800, 100) + token_count(1000, 800, 100) + token_count(1500, 1000, 150))
        self.assertEqual(got, [USAGE, {'input_tokens': 300, 'output_tokens': 50, 'cache_read_input_tokens': 200, 'cache_creation_input_tokens': 0}])

    def test_records_win_over_the_running_total(self):
        got = self.fold(context() + usage_record() + token_count(1000, 800, 100) + usage_record() + token_count(2000, 1600, 200))
        self.assertEqual(got, [USAGE, USAGE])

    def test_total_going_back_after_a_rollback_is_not_new_spending(self):
        got = self.fold(context() + token_count(5000, 0, 500) + token_count(3000, 0, 300) + token_count(3100, 0, 310))
        self.assertEqual([u['input_tokens'] for u in got], [5000, 100])

    def test_fast_tier_is_kept(self):
        got = self.fold(context() + event('thread_settings_applied', thread_settings={'service_tier': 'fast'}) + usage_record())
        self.assertEqual(got[0]['speed'], 'fast')

    def test_model_known_before_the_first_turn(self):
        meter = {}
        settings = event('thread_settings_applied', thread_settings={'model': 'gpt-5.5'})
        for raw in (settings + token_count(10, 0, 1)).splitlines():
            server.codex_usage(meter, json.loads(raw))
        self.assertEqual(server.codex_model(meter), 'gpt-5.5')

    def test_review_thread_after_compaction_is_still_the_review_model(self):
        meter = {}
        text = meta(thread='child', source={'subagent': {'other': 'guardian'}}) + token_count(10, 0, 1)
        for raw in text.splitlines():
            server.codex_usage(meter, json.loads(raw))
        self.assertEqual(server.codex_model(meter), 'codex-auto-review')

    def test_provider_by_model(self):
        self.assertEqual([server.provider_of(m) for m in ('claude-opus-5-5', 'gpt-6-astra', 'codex-auto-review', 'gpt-?')],
                         ['claude', 'codex', 'codex', 'codex'])


class CodexTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'rollout-2026-01-01T10-00-00-%s.jsonl' % THREAD)

    def tearDown(self):
        self.dir.cleanup()

    def tail(self, text):
        with open(self.path, 'w') as f:
            f.write(text)
        return server.CodexTail(self.path).feed()

    def test_thread_folder_prompt_and_cost(self):
        t = self.tail(meta(cwd='/work/app') + context() + item('UserMessage', content=[{'type': 'text', 'text': 'почини сборку'}]) + usage_record())
        self.assertEqual((t.thread, t.home, t.prompt, t.model), (THREAD, '/work/app', 'почини сборку', 'gpt-5.6-sol'))
        self.assertAlmostEqual(t.cost, server.message_cost('gpt-5.6-sol', USAGE))

    def test_turn_marks_busy_until_it_ends(self):
        self.assertTrue(self.tail(meta() + event('task_started')).busy)
        self.assertFalse(self.tail(meta() + event('task_started') + event('task_complete')).busy)
        self.assertFalse(self.tail(meta() + event('task_started') + event('turn_aborted')).busy)

    def test_work_is_the_tasks_not_the_pause_between_them(self):
        t = self.tail(meta() + event('task_started', ts='2026-01-01T10:00:00.000Z') + event('task_complete', ts='2026-01-01T10:01:00.000Z')
                      + usage_record(ts='2026-01-01T10:30:00.000Z')
                      + event('task_started', ts='2026-01-02T09:00:00.000Z') + usage_record(ts='2026-01-02T09:02:00.000Z'))
        self.assertEqual(t.worked(0, False), [
            [server.ms('2026-01-01T10:00:00.000Z'), server.ms('2026-01-01T10:01:00.000Z')],
            [server.ms('2026-01-02T09:00:00.000Z'), server.ms('2026-01-02T09:02:00.000Z')]])

    def test_abandoned_task_does_not_stretch_into_the_next(self):
        t = self.tail(meta() + event('task_started', ts='2026-01-01T10:00:00.000Z') + usage_record(ts='2026-01-01T10:01:00.000Z')
                      + event('task_started', ts='2026-01-01T12:00:00.000Z') + usage_record(ts='2026-01-01T12:01:00.000Z'))
        self.assertEqual(t.worked(0, False), [
            [server.ms('2026-01-01T10:00:00.000Z'), server.ms('2026-01-01T10:01:00.000Z')],
            [server.ms('2026-01-01T12:00:00.000Z'), server.ms('2026-01-01T12:01:00.000Z')]])

    def test_resumed_session_does_not_stretch_an_abandoned_task(self):
        back = '2026-01-01T12:00:00.000Z'
        t = self.tail(meta() + event('task_started', ts='2026-01-01T10:00:00.000Z') + usage_record(ts='2026-01-01T10:01:00.000Z')
                      # all of these come between tasks: none of them means the old one is still going
                      + rec('session_meta', {'id': THREAD}, back) + rec('world_state', {}, back) + rec('compacted', {}, back)
                      + rec('turn_context', {'model': 'gpt-5.6-sol'}, back) + event('thread_settings_applied', back) + event('token_count', back)
                      + rec('response_item', {'type': 'message', 'role': 'developer', 'content': []}, back)
                      + rec('response_item', {'type': 'compaction'}, back)
                      # nor do the kinds Codex has yet to invent
                      + rec('something_new', {}, back) + event('something_new', back) + rec('response_item', {'type': 'something_new'}, back)
                      + event('task_started', ts='2026-01-01T12:00:30.000Z') + usage_record(ts='2026-01-01T12:01:00.000Z'))
        self.assertEqual(t.worked(0, False), [
            [server.ms('2026-01-01T10:00:00.000Z'), server.ms('2026-01-01T10:01:00.000Z')],
            [server.ms('2026-01-01T12:00:30.000Z'), server.ms('2026-01-01T12:01:00.000Z')]])

    def test_long_tool_call_keeps_running_until_its_output(self):
        ts = '2026-01-01T10:00:00.000Z'
        for call, output in (('custom_tool_call', 'custom_tool_call_output'), ('function_call', 'function_call_output')):
            calling = meta() + event('task_started', ts=ts) + rec('response_item', {'type': call, 'call_id': 'c1', 'name': 'exec', 'input': ''}, ts)
            t = self.tail(calling)
            now = server.ms(ts) + (server.LIVE_S + 300) * 1000
            self.assertEqual(t.worked(now), [[server.ms(ts), now]], call)
            t = self.tail(calling + rec('response_item', {'type': output, 'call_id': 'c1', 'output': 'ok'}, '2026-01-01T10:01:00.000Z'))
            self.assertEqual(t.worked(now), [[server.ms(ts), server.ms('2026-01-01T10:01:00.000Z')]], output)

    def test_finished_task_forgets_its_calls(self):
        ts = '2026-01-01T10:00:00.000Z'
        t = self.tail(meta() + event('task_started', ts=ts) + rec('response_item', {'type': 'function_call', 'call_id': 'c1', 'name': 'exec', 'arguments': ''}, ts)
                      + event('turn_aborted', ts='2026-01-01T10:01:00.000Z') + event('task_started', ts='2026-01-01T10:02:00.000Z'))
        self.assertEqual(t.calling_tools, {})

    def test_turn_starts_at_the_last_task(self):
        t = self.tail(meta() + event('task_started', ts='2026-01-01T10:00:00.000Z') + event('task_complete', ts='2026-01-01T10:01:00.000Z')
                      + event('task_started', ts='2026-01-02T09:00:00.000Z'))
        self.assertEqual(t.turn, server.ms('2026-01-02T09:00:00.000Z'))

    def test_finished_items_read_as_actions(self):
        t = self.tail(meta()
                      + item('CommandExecution', command=['/bin/zsh', '-lc', 'npm test'], parsed_cmd=[{'type': 'unknown', 'cmd': 'npm test'}])
                      + item('CommandExecution', command=['/bin/zsh', '-lc', 'cat a/b.py'], parsed_cmd=[{'type': 'read', 'cmd': 'cat a/b.py', 'name': 'b.py', 'path': 'a/b.py'}])
                      + item('CommandExecution', command=['/bin/zsh', '-lc', 'rg foo'], parsed_cmd=[{'type': 'search', 'cmd': 'rg foo', 'query': 'foo'}])
                      + item('FileChange', changes={'/work/app/x.ts': {'type': 'update'}}, status='completed')
                      + item('FileChange', changes={'/work/app/new.ts': {'type': 'add'}}, status='completed')
                      + item('WebSearch', query='что-то')
                      + item('McpToolCall', server='notion', tool='search', status='completed')
                      + item('ImageView', path='/tmp/shot.png'))
        self.assertEqual([what for _, what in t.events],
                         ['$ npm test', 'читает b.py', 'ищет foo', 'правит x.ts', 'пишет new.ts', 'гуглит', 'листает Notion', 'читает shot.png'])
        self.assertEqual(t.calls, 8)
        self.assertEqual(t.tool, 'read')

    def test_call_in_progress_is_seen_before_it_finishes(self):
        call = rec('response_item', {'type': 'custom_tool_call', 'name': 'exec',
                                     'input': 'const r = await tools.exec_command({cmd:"npm run build",yield_time_ms:1000});'})
        t = self.tail(meta() + call)
        self.assertEqual((t.activity, t.tool), ('$ npm run', 'bash'))
        self.assertEqual(t.calls, 0)  # посчитается, когда вызов закончится

    def test_review_thread_knows_its_parent(self):
        t = self.tail(meta(thread='child', source={'subagent': {'other': 'guardian'}}, parent_thread_id=THREAD))
        self.assertEqual((t.parent, t.role), (THREAD, 'guardian'))

    def test_spawned_thread_knows_its_parent(self):
        t = self.tail(meta(thread='child', source={'subagent': {'thread_spawn': {'parent_thread_id': THREAD}}}, agent_nickname='Maxwell'))
        self.assertEqual((t.parent, t.role, t.nickname), (THREAD, 'general-purpose', 'Maxwell'))


class CodexSessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = server.PROJECTS, server.CODEX_SESSIONS, server.CODEX_INDEX, server.DATA
        server.PROJECTS = os.path.join(self.dir.name, 'claude')
        server.CODEX_SESSIONS = os.path.join(self.dir.name, 'sessions')
        server.CODEX_INDEX = os.path.join(self.dir.name, 'session_index.jsonl')
        server.DATA = self.dir.name
        os.makedirs(server.PROJECTS)
        os.makedirs(server.CODEX_SESSIONS)
        server.TAILS.clear()

    def tearDown(self):
        server.PROJECTS, server.CODEX_SESSIONS, server.CODEX_INDEX, server.DATA = self.saved
        server.TAILS.clear()
        self.dir.cleanup()

    def write(self, thread, text):
        day = os.path.join(server.CODEX_SESSIONS, '2026', '01', '01')
        os.makedirs(day, exist_ok=True)
        with open(os.path.join(day, 'rollout-2026-01-01T10-00-00-%s.jsonl' % thread), 'w') as f:
            f.write(text)

    def now(self, offset=0):
        return iso(time.time() + offset)

    def test_session_is_listed_with_its_provider_and_title(self):
        self.write(THREAD, meta() + context(ts=self.now()) + event('task_started', self.now()) + usage_record(ts=self.now()))
        with open(server.CODEX_INDEX, 'w') as f:
            f.write(json.dumps({'id': THREAD, 'thread_name': 'Разобрать события'}) + '\n')
        [s] = server.sessions()
        self.assertEqual((s['provider'], s['title'], s['project'], s['id']), ('codex', 'Разобрать события', 'app', THREAD))
        self.assertEqual(s['working'], 1)
        self.assertTrue(s['isLive'])
        self.assertAlmostEqual(s['today'], round(server.message_cost('gpt-5.6-sol', USAGE), 2))

    def test_prompt_is_the_title_until_codex_names_the_thread(self):
        self.write(THREAD, meta() + item('UserMessage', self.now(), content=[{'type': 'text', 'text': 'почини сборку'}]))
        self.assertEqual(server.sessions()[0]['title'], 'почини сборку')

    def test_touched_file_without_new_records_is_not_a_recent_session(self):
        self.write(THREAD, meta() + context(ts=iso(time.time() - 3 * 86400)))
        self.assertEqual(server.sessions(), [])

    def test_review_thread_works_at_its_parents_desk(self):
        self.write(THREAD, meta() + context(ts=self.now()) + usage_record(ts=self.now()))
        self.write('child', meta(thread='child', source={'subagent': {'other': 'guardian'}}, parent_thread_id=THREAD)
                   + context('codex-auto-review', ts=self.now()) + event('task_started', self.now()) + usage_record(ts=self.now()))
        [s] = server.sessions()
        self.assertEqual((s['id'], s['agents'], s['working']), (THREAD, 1, 1))
        st = server.state(THREAD)
        [a] = st['agents']
        self.assertEqual((a['type'], a['model'], a['isWorking']), ('guardian', 'codex-auto-review', True))
        self.assertEqual([d['name'] for z in st['office']['zones'] for d in z['desks']], ['Главный', 'Контролёр'])

    def test_work_time_joins_a_session_and_its_helpers(self):
        now = time.time()
        self.write(THREAD, meta() + event('task_started', iso(now - 300)) + event('task_complete', iso(now - 200)))
        self.write('child', meta(thread='child', source={'subagent': {'other': 'guardian'}}, parent_thread_id=THREAD)
                   + event('task_started', iso(now - 250)) + event('task_complete', iso(now - 100)))
        self.assertEqual(server.sessions()[0]['worked'], 200000)
        st = server.state(THREAD)
        self.assertEqual((st['worked'], st['workingSince']), (200000, 0))
        self.assertNotIn('spans', st['agents'][0])

    def test_codex_office_has_its_own_sign_and_no_generation(self):
        self.write(THREAD, meta() + context(ts=self.now()) + event('task_started', self.now()))
        st = server.state(THREAD)
        self.assertEqual(st['office']['provider'], 'codex')
        self.assertEqual(st['office']['accent'], server.CODEX_ACCENT)
        self.assertEqual(st['office']['title'], 'ОФИС · APP')
        self.assertTrue(st['producer']['isBusy'])
        self.assertEqual(st['producer']['model'], 'gpt-5.6-sol')

    def test_folder_with_a_claude_office_stays_codex_inside_codex_session(self):
        os.makedirs(os.path.dirname(server.office_path('/work/app')), exist_ok=True)
        with open(server.office_path('/work/app'), 'w') as f:
            json.dump({'folder': '/work/app', 'title': 'СТУДИЯ', 'zones': [], 'guests': {}}, f)
        self.write(THREAD, meta() + context(ts=self.now()))
        self.assertEqual(server.state(THREAD)['office']['title'], 'ОФИС · APP')

    def test_no_codex_folder_is_fine(self):
        server.CODEX_SESSIONS = os.path.join(self.dir.name, 'нет')
        self.assertEqual(server.sessions(), [])

    def test_no_claude_folder_still_shows_codex(self):
        server.PROJECTS = os.path.join(self.dir.name, 'нет')
        self.write(THREAD, meta() + context(ts=self.now()))
        self.assertEqual([s['provider'] for s in server.sessions()], ['codex'])
        self.assertIsNotNone(server.state(THREAD))
        self.assertIsNone(server.find_tail('нет-такой'))

    def test_no_claude_folder_still_gives_stats(self):
        server.PROJECTS = os.path.join(self.dir.name, 'нет')
        self.write(THREAD, meta() + context(ts=self.now()) + usage_record(ts=self.now()))
        with mock.patch.object(server, 'LEDGER', None):
            report = server.stats()
        self.assertEqual(list(report['days'][-1]['models']), ['gpt-5.6-sol'])


class LedgerCodexTest(unittest.TestCase):
    usd = server.message_cost('gpt-5.6-sol', USAGE)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.dir.name, 'claude')
        self.codex = os.path.join(self.dir.name, 'sessions')
        os.makedirs(os.path.join(self.claude, '-work-app'))
        self.day = os.path.join(self.codex, '2026', '01', '01')
        os.makedirs(self.day)
        self.path = os.path.join(self.day, 'rollout-2026-01-01T10-00-00-%s.jsonl' % THREAD)
        self.cache = os.path.join(self.dir.name, 'stats.json')
        self.at = iso(local_day(0) + 3600)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text, mode='w', path=None):
        with open(path or self.path, mode) as f:
            f.write(text)

    def scan(self, ledger=None):
        ledger = ledger or server.Ledger(self.cache)
        ledger.scan(self.claude, self.codex)
        return ledger

    def zstd(self):
        exe = server.find_zstd()
        if not exe:
            self.skipTest('нет zstd')
        import subprocess
        subprocess.run([exe, '-q', '--rm', '-f', self.path], check=True)

    def session(self):
        return meta() + context(ts=self.at) + usage_record(ts=self.at)

    def test_no_claude_folder_still_counts_codex(self):
        self.write(self.session())
        self.claude = os.path.join(self.dir.name, 'нет')
        self.assertEqual(list(self.scan().report()['days'][-1]['models']), ['gpt-5.6-sol'])

    def test_codex_spending_lands_in_the_same_report(self):
        self.write(self.session())
        with open(os.path.join(self.claude, '-work-app', 'sid.jsonl'), 'w') as f:
            f.write(assistant('m1', ts=self.at))
        today = self.scan().report()['days'][-1]
        self.assertEqual(set(today['models']), {'gpt-5.6-sol', 'claude-sonnet-5-5'})
        self.assertAlmostEqual(today['projects']['app'], self.usd + server.message_cost('claude-sonnet-5-5', {'input_tokens': 1000, 'output_tokens': 100}), delta=1e-4)  # отчёт округляет до 4 знаков
        self.assertEqual(today['tokParts'][:3], [1200, 200, 800])
        self.assertEqual((today['sessions'], today['started']), (2, 2))

    def test_report_for_one_provider(self):
        self.write(self.session())
        with open(os.path.join(self.claude, '-work-app', 'sid.jsonl'), 'w') as f:
            f.write(assistant('m1', ts=iso(local_day(-2) + 3600)))
        ledger = self.scan()
        codex = ledger.report(provider='codex')
        self.assertEqual(codex['since'], server.day_of(local_day(0) * 1000))
        self.assertEqual(list(codex['days'][-1]['models']), ['gpt-5.6-sol'])
        self.assertEqual((codex['days'][-1]['sessions'], codex['days'][-1]['started']), (1, 1))
        self.assertAlmostEqual(codex['days'][-1]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков
        self.assertEqual([h[2] for h in codex['hours']], [round(self.usd, 4)])
        claude = ledger.report(provider='claude')
        self.assertEqual(claude['since'], server.day_of(local_day(-2) * 1000))
        self.assertEqual(claude['days'][-1]['usd'], 0)
        self.assertEqual(set(ledger.report()['first']), {'claude', 'codex'})
        self.assertEqual(ledger.report()['first']['codex'], server.day_of(local_day(0) * 1000))

    def test_review_thread_pays_but_is_not_a_session(self):
        self.write(meta(thread='child', source={'subagent': {'other': 'guardian'}}, parent_thread_id=THREAD)
                   + context('codex-auto-review', ts=self.at) + usage_record(ts=self.at))
        today = self.scan().report()['days'][-1]
        self.assertEqual(today['tokModels'], {'codex-auto-review': 1100})
        self.assertEqual((today['sessions'], today['started']), (0, 0))
        self.assertEqual(today['usd'], 0)  # цены нет в прайсе

    def test_chatgpt_project_folder_gets_a_readable_name(self):
        self.write(meta(cwd='/u/.codex/.chatgpt-projects/g-p-6aa15ef21e70') + context(cwd='/u/.codex/.chatgpt-projects/g-p-6aa15ef21e70', ts=self.at) + usage_record(ts=self.at))
        self.assertEqual(list(self.scan().report()['days'][-1]['projects']), ['ChatGPT-проект'])

    def test_appended_lines_are_read_once(self):
        self.write(self.session())
        ledger = self.scan()
        self.write(usage_record(ts=self.at), mode='a')
        self.scan(ledger)
        self.scan(ledger)
        self.assertAlmostEqual(ledger.report()['days'][-1]['usd'], 2 * self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_compressed_journal_is_read(self):
        self.write(self.session())
        self.zstd()
        self.assertAlmostEqual(self.scan().report()['days'][-1]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_compressing_a_journal_does_not_count_it_twice(self):
        self.write(self.session())
        ledger = self.scan()
        ledger.save()
        self.zstd()
        again = server.Ledger(self.cache)
        self.scan(again)
        self.scan(again)
        self.assertAlmostEqual(again.report()['days'][-1]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_journal_resumed_after_compression_adds_only_the_new_part(self):
        self.write(self.session())
        ledger = self.scan()
        self.zstd()
        self.scan(ledger)
        import subprocess
        subprocess.run([server.find_zstd(), '-q', '-d', '--rm', '-f', self.path + '.zst'], check=True)
        self.write(usage_record(ts=self.at), mode='a')
        self.scan(ledger)
        self.assertAlmostEqual(ledger.report()['days'][-1]['usd'], 2 * self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_without_zstd_compressed_journals_wait(self):
        self.write(self.session())
        self.zstd()
        find, server.find_zstd = server.find_zstd, lambda: None
        try:
            ledger = self.scan()
            self.assertEqual(ledger.report()['days'], [])
        finally:
            server.find_zstd = find
        self.assertAlmostEqual(self.scan(ledger).report()['days'][-1]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_old_stats_file_keeps_claude_and_gains_codex(self):
        hour = time.strftime('%Y-%m-%dT%H', time.localtime(local_day(-1) + 3600))
        cell = [1.0, 0.0, 0.0, 0.0, 1100, 1000, 100, 0, 0]
        self.write(json.dumps({'v': server.Ledger.VERSION, 'files': {}, 'hours': {hour: {'app': {'claude-opus-5-5': cell}}}}), path=self.cache)
        self.write(self.session())
        days = {d['day']: d for d in self.scan().report()['days']}
        self.assertEqual(days[hour[:10]]['usd'], 1.0)
        self.assertAlmostEqual(days[server.day_of(local_day(0) * 1000)]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_latest_limits_survive_a_restart(self):
        old = {'limit_id': 'codex', 'plan_type': 'plus', 'primary': {'used_percent': 90.0, 'window_minutes': 300, 'resets_at': 100}}
        new = {'limit_id': 'codex', 'plan_type': 'plus', 'primary': {'used_percent': 24.0, 'window_minutes': 300, 'resets_at': 200},
               'secondary': {'used_percent': 8.0, 'window_minutes': 10080, 'resets_at': 300}}
        self.write(meta() + context(ts=self.at) + token_count(10, 0, 1, ts='2026-01-02T10:00:00.000Z', limits=new)
                   + token_count(11, 0, 1, ts='2026-01-01T10:00:00.000Z', limits=old))
        ledger = self.scan()
        ledger.save()
        limits = server.Ledger(self.cache).report()['limits']
        self.assertEqual(limits['plan'], 'plus')
        self.assertEqual([(w['minutes'], w['used'], w['resets']) for w in limits['windows']], [(300, 24.0, 200), (10080, 8.0, 300)])


FAKE_OK = '''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False, "structured_output": {
    "title": "Новый", "boss": {"name": "Тимлид", "prop": "crown"},
    "zones": [{"name": "код", "desks": [{"type": "kit:builder", "name": "Сборщик", "prop": "box"}]}], "guests": []}}))
'''
FAKE_LOGGED_OUT = '''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}))
sys.exit(1)
'''
FAKE_STUCK = '''#!/usr/bin/env python3
import time
time.sleep(30)
'''


FAKE_ORPHAN = '''#!/bin/sh
sleep 30 &
echo $! > "$0.child"
wait
'''
FAKE_NO_NETWORK = '''#!/usr/bin/env python3
import json, sys, time
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
print(json.dumps({"type": "system", "subtype": "api_retry", "error": "unknown"}), flush=True)
print(json.dumps({"type": "system", "subtype": "api_retry", "error": "unknown"}), flush=True)
time.sleep(30)
'''
FAKE_SILENT = '''#!/usr/bin/env python3
import json, sys, time
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
time.sleep(30)
'''


class GenerationTest(unittest.TestCase):
    roster = {'kit:builder': '', 'Explore': ''}
    folder = '/work/app'

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.data, server.DATA = server.DATA, self.dir.name
        self.find, self.timeout, self.quiet = server.find_claude, server.GEN_TIMEOUT, server.GEN_QUIET
        self.config, server.CLAUDE_DIR = server.CLAUDE_DIR, self.dir.name
        self.logs, server.PROJECTS = server.PROJECTS, os.path.join(self.dir.name, 'нет')
        server.JOBS.clear()

    def tearDown(self):
        server.stop_jobs()
        for job in list(server.JOBS.values()):
            for _ in range(100):
                if job['status'] != 'running':
                    break
                time.sleep(0.02)
        server.DATA, server.find_claude, server.GEN_TIMEOUT, server.GEN_QUIET = self.data, self.find, self.timeout, self.quiet
        server.CLAUDE_DIR, server.PROJECTS = self.config, self.logs
        self.dir.cleanup()

    def fake(self, body):
        path = os.path.join(self.dir.name, 'claude')
        with open(path, 'w') as f:
            f.write(body)
        os.chmod(path, 0o755)
        server.find_claude = lambda: path

    def saved(self, title):
        os.makedirs(os.path.dirname(server.office_path(self.folder)), exist_ok=True)
        with open(server.office_path(self.folder), 'w') as f:
            json.dump({'folder': self.folder, 'title': title, 'zones': [], 'guests': {}}, f)

    def wait(self):
        for _ in range(100):
            view = server.job_view(self.folder)
            if view['status'] != 'running':
                return view
            time.sleep(0.05)
        self.fail('generation did not finish')

    def test_generation_runs_in_background_and_keeps_the_old_office_for_undo(self):
        self.fake(FAKE_OK)
        self.saved('СТАРЫЙ')
        server.start_office(self.folder, self.roster)
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(server.load_office(self.folder)['title'], 'НОВЫЙ')
        self.assertTrue(server.job_view(self.folder)['canUndo'])
        self.assertTrue(server.undo_office(self.folder))
        self.assertEqual(server.load_office(self.folder)['title'], 'СТАРЫЙ')
        self.assertFalse(server.job_view(self.folder)['canUndo'])

    def test_failure_names_the_reason_and_leaves_the_office_alone(self):
        self.fake(FAKE_LOGGED_OUT)
        self.saved('СТАРЫЙ')
        server.start_office(self.folder, self.roster)
        view = self.wait()
        self.assertEqual(view['status'], 'failed')
        self.assertIn('Not logged in', view['error'])
        self.assertEqual(server.load_office(self.folder)['title'], 'СТАРЫЙ')

    def test_no_office_is_written_when_generation_fails(self):
        self.fake(FAKE_LOGGED_OUT)
        server.start_office(self.folder, self.roster)
        self.wait()
        self.assertIsNone(server.load_office(self.folder))

    def test_cancel_stops_claude(self):
        self.fake(FAKE_STUCK)
        self.saved('СТАРЫЙ')
        server.start_office(self.folder, self.roster)
        time.sleep(0.3)
        self.assertTrue(server.cancel_office(self.folder))
        self.assertEqual(self.wait()['status'], 'cancelled')
        self.assertEqual(server.load_office(self.folder)['title'], 'СТАРЫЙ')

    def test_timeout_explains_what_to_check(self):
        self.fake(FAKE_STUCK)
        server.GEN_TIMEOUT = 0.5
        server.start_office(self.folder, self.roster)
        view = self.wait()
        self.assertEqual(view['status'], 'failed')
        self.assertIn('/login', view['error'])

    def test_second_start_while_running_is_refused(self):
        self.fake(FAKE_STUCK)
        server.start_office(self.folder, self.roster)
        self.assertIsNone(server.start_office(self.folder, self.roster))

    def child_of_fake(self):
        path = server.find_claude() + '.child'
        for _ in range(100):
            if os.path.exists(path) and open(path).read().strip():
                return int(open(path).read())
            time.sleep(0.02)
        self.fail('fake claude did not start its child')

    def alive(self, pid):
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            time.sleep(0.02)
        return True

    def test_cancel_takes_the_children_of_claude_too(self):
        self.fake(FAKE_ORPHAN)
        server.start_office(self.folder, self.roster)
        child = self.child_of_fake()
        self.assertTrue(server.cancel_office(self.folder))
        self.assertEqual(self.wait()['status'], 'cancelled')
        self.assertFalse(self.alive(child))

    def test_timeout_does_not_wait_for_the_children_of_claude(self):
        self.fake(FAKE_ORPHAN)
        server.GEN_TIMEOUT = 0.3
        server.start_office(self.folder, self.roster)
        child = self.child_of_fake()
        self.assertEqual(self.wait()['status'], 'failed')
        self.assertFalse(self.alive(child))

    def test_retries_against_the_api_fail_at_once_and_name_the_network(self):
        self.fake(FAKE_NO_NETWORK)
        server.start_office(self.folder, self.roster)
        view = self.wait()
        self.assertEqual(view['status'], 'failed')
        self.assertIn('прокси', view['error'])

    def test_silence_after_the_start_is_a_network_failure_too(self):
        self.fake(FAKE_SILENT)
        server.GEN_QUIET = 0.3
        server.start_office(self.folder, self.roster)
        view = self.wait()
        self.assertEqual(view['status'], 'failed')
        self.assertIn('прокси', view['error'])

    def test_stage_tells_what_generation_waits_for(self):
        self.fake(FAKE_SILENT)
        server.start_office(self.folder, self.roster)
        for _ in range(100):
            if server.job_view(self.folder)['stage'] == 'answer':
                return
            time.sleep(0.02)
        self.fail('stage never moved to answer')

    def test_generations_go_one_at_a_time(self):
        self.fake(FAKE_STUCK)
        server.start_office(self.folder, self.roster)
        server.start_office('/work/other', self.roster)
        time.sleep(0.2)
        self.assertEqual(server.job_view('/work/other')['stage'], 'queue')
        server.cancel_office('/work/other')
        for _ in range(100):
            if server.job_view('/work/other')['status'] == 'cancelled':
                return
            time.sleep(0.02)
        self.fail('queued generation was not cancelled')

    def test_stopping_the_server_stops_claude(self):
        self.fake(FAKE_ORPHAN)
        server.start_office(self.folder, self.roster)
        child = self.child_of_fake()
        server.stop_jobs()
        self.assertFalse(self.alive(child))

    def test_failed_write_keeps_the_office(self):
        self.saved('СТАРЫЙ')
        real, server.json.dump = server.json.dump, lambda *a, **kw: 1 / 0
        try:
            with self.assertRaises(ZeroDivisionError):
                server.write_office(self.folder, {'title': 'НОВЫЙ'})
        finally:
            server.json.dump = real
        self.assertEqual(server.load_office(self.folder)['title'], 'СТАРЫЙ')


AGENT_MD = '''---
name: %s
description: %s
model: opus
---

# %s

Находит лучшие куски выпуска и передаёт их Сценаристу.
'''


class Team:
    """Папка конфигурации Claude Code с плагинами и агентами."""

    def __init__(self, root):
        self.root = root
        self.claude = os.path.join(root, 'claude')
        self.projects = os.path.join(self.claude, 'projects')
        self.folder = os.path.join(root, 'work', 'app')
        os.makedirs(self.projects)
        os.makedirs(os.path.join(self.folder, '.claude'))
        self.installed, self.enabled = {}, {}
        self.saved = server.CLAUDE_DIR, server.PROJECTS, server.DATA
        server.CLAUDE_DIR, server.PROJECTS, server.DATA = self.claude, self.projects, os.path.join(root, 'data')
        self.flush()

    def close(self):
        server.CLAUDE_DIR, server.PROJECTS, server.DATA = self.saved

    def flush(self):
        os.makedirs(os.path.join(self.claude, 'plugins'), exist_ok=True)
        with open(os.path.join(self.claude, 'plugins', 'installed_plugins.json'), 'w') as f:
            json.dump({'plugins': self.installed}, f)
        with open(os.path.join(self.claude, 'settings.json'), 'w') as f:
            json.dump({'enabledPlugins': self.enabled}, f)

    def plugin(self, name, agents, scope='user', enabled=True, skill=''):
        path = os.path.join(self.root, 'plugins', name)
        os.makedirs(os.path.join(path, 'agents'))
        for key, (role, desc) in agents.items():
            with open(os.path.join(path, 'agents', key + '.md'), 'w') as f:
                f.write(AGENT_MD % (key, desc, role))
        if skill:
            os.makedirs(os.path.join(path, 'skills', 'lead'))
            with open(os.path.join(path, 'skills', 'lead', 'SKILL.md'), 'w') as f:
                f.write(skill)
        entry = {'scope': scope, 'installPath': path}
        if scope == 'project':
            entry['projectPath'] = self.folder
        self.installed[name + '@market'] = [entry]
        self.enabled[name + '@market'] = enabled
        self.flush()

    def session(self, sid, roster, age=0, cwd=None):
        pdir = os.path.join(self.projects, re.sub(r'[^A-Za-z0-9]', '-', cwd or self.folder))
        os.makedirs(pdir, exist_ok=True)
        path = os.path.join(pdir, sid + '.jsonl')
        with open(path, 'w') as f:
            f.write(line(type='attachment', attachment={
                'type': 'agent_listing_delta', 'addedTypes': list(roster),
                'addedLines': ['- %s: %s' % (t, d) for t, d in roster.items()]}))
            f.write(line(type='user', timestamp=iso(time.time() - age), cwd=cwd or self.folder, message={'content': 'привет'}))
        os.utime(path, (time.time() - age, time.time() - age))
        return pdir, path

    def called(self, pdir, sid, kinds):
        sub = os.path.join(pdir, sid, 'subagents')
        os.makedirs(sub, exist_ok=True)
        for i, kind in enumerate(kinds):
            with open(os.path.join(sub, 'agent-%d.jsonl' % i), 'w') as f:
                f.write(line(type='user', timestamp=iso(1000 + i), cwd=self.folder, message={'content': 'x'}))
            with open(os.path.join(sub, 'agent-%d.meta.json' % i), 'w') as f:
                json.dump({'agentType': kind}, f)


class TeamCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.team = Team(self.dir.name)
        self.folder = self.team.folder
        server.TAILS.clear()
        server.JOBS.clear()
        server.AUTO_TRIED.clear()

    def tearDown(self):
        server.stop_jobs()
        for job in list(server.JOBS.values()):
            for _ in range(100):
                if job['status'] != 'running':
                    break
                time.sleep(0.02)
        self.team.close()
        server.TAILS.clear()
        self.dir.cleanup()

    def seats(self, office):
        return {d['type']: z['name'] for z in office['zones'] for d in z['desks']}


class AgentFilesTest(TeamCase):
    def test_plugin_agent_brings_role_stage_and_instructions(self):
        self.team.plugin('reels', {'scout': ('Скаут', 'Скаут конвейера — план.')}, scope='project')
        info = server.agent_files(self.folder)['reels:scout']
        self.assertEqual((info['role'], info['desc'], info['model'], info['scope']), ('Скаут', 'Скаут конвейера — план.', 'opus', 'project'))
        self.assertIn('Находит лучшие куски', info['prompt'])

    def test_disabled_plugin_has_no_staff(self):
        self.team.plugin('off', {'scout': ('Скаут', 'x')}, enabled=False)
        self.assertEqual(server.agent_files(self.folder), {})

    def test_plugin_of_another_project_has_no_staff_here(self):
        self.team.plugin('alien', {'scout': ('Скаут', 'x')}, scope='project')
        self.team.installed['alien@market'][0]['projectPath'] = '/work/other'
        self.team.flush()
        self.assertEqual(server.agent_files(self.folder), {})

    def test_project_setting_enables_a_plugin(self):
        self.team.plugin('kit', {'scout': ('Скаут', 'x')}, scope='project', enabled=False)
        with open(os.path.join(self.folder, '.claude', 'settings.json'), 'w') as f:
            json.dump({'enabledPlugins': {'kit@market': True}}, f)
        self.assertIn('kit:scout', server.agent_files(self.folder))

    def test_loose_agents_of_project_and_user(self):
        for root, name in ((os.path.join(self.folder, '.claude'), 'near'), (self.team.claude, 'far')):
            os.makedirs(os.path.join(root, 'agents'), exist_ok=True)
            with open(os.path.join(root, 'agents', name + '.md'), 'w') as f:
                f.write(AGENT_MD % (name, 'x', name))
        files = server.agent_files(self.folder)
        self.assertEqual((files['near']['scope'], files['far']['scope']), ('project', 'user'))


class StaffTest(TeamCase):
    def test_newest_session_of_the_folder_is_the_staff(self):
        self.team.session('old', {'kit:a': 'старый'}, age=500)
        self.team.session('new', {'kit:a': 'новый', 'kit:b': ''}, age=5)
        self.assertEqual(server.newest_roster(self.folder), {'kit:a': 'новый', 'kit:b': ''})

    def test_session_without_a_list_does_not_empty_the_staff(self):
        self.team.session('old', {'kit:a': ''}, age=500)
        self.team.session('new', {}, age=5)
        self.assertEqual(list(server.newest_roster(self.folder)), ['kit:a'])

    def test_worktree_sessions_count(self):
        self.team.session('wt', {'kit:a': ''}, cwd=self.folder + '/.claude/worktrees/x')
        self.assertEqual(list(server.newest_roster(self.folder)), ['kit:a'])

    def test_agent_from_a_file_joins_before_any_session_lists_it(self):
        self.team.plugin('kit', {'b': ('Новый', 'из файла')}, scope='project')
        roster, files = server.staff_of(self.folder, {'kit:a': 'из журнала'})
        self.assertEqual(roster, {'kit:a': 'из журнала', 'kit:b': 'из файла'})
        self.assertIn('kit:b', files)

    def test_usage_counts_calls_and_order(self):
        pdir, _ = self.team.session('s', {'kit:a': ''})
        self.team.called(pdir, 's', ['kit:a', 'kit:b', 'kit:a'])
        counts, pairs = server.usage_of(self.folder)
        self.assertEqual(counts['kit:a'], 2)
        self.assertEqual(pairs[('kit:a', 'kit:b')], 1)


def raw_office(zones, boss='Тимлид', title='Команда'):
    return {'title': title, 'boss': {'name': boss, 'prop': 'crown'}, 'guests': [],
            'zones': [{'name': name, 'desks': desks} for name, desks in zones]}


def desk(t, name, short=None, prop='box'):
    return dict(type=t, name=name, short=short or name, prop=prop)


class MeaningTest(TeamCase):
    def test_role_from_the_agent_file_beats_the_model_and_the_old_name(self):
        files = {'k:m': {'role': 'Моушн-дизайнер', 'scope': 'project', 'plugin': 'k'}}
        old = {'zones': [{'name': 'МОНТАЖ', 'desks': [{'type': 'k:m', 'name': 'Моушн', 'prop': 'box'}]}], 'guests': {}}
        got = server.tidy(self.folder, {'k:m': ''}, raw_office([('Монтаж', [desk('k:m', 'Аниматор', 'Моушн')])]), old, files)
        d = [d for d in got['zones'][0]['desks'] if d['type'] == 'k:m'][0]
        self.assertEqual((d['name'], d['short']), ('Моушн-дизайнер', 'Моушн'))

    def test_old_name_stays_when_no_file_names_the_agent(self):
        old = {'zones': [{'name': 'КОД', 'desks': [{'type': 'k:b', 'name': 'Правщик', 'prop': 'box'}]}], 'guests': {}}
        got = server.tidy(self.folder, {'k:b': ''}, raw_office([('Код', [desk('k:b', 'Сборщик')])]), old)
        self.assertIn('Правщик', [d['name'] for d in got['zones'][0]['desks']])

    def test_locked_desk_keeps_its_floor_and_name(self):
        old = {'zones': [{'id': 'моё', 'name': 'МОЁ', 'desks': [{'type': 'k:b', 'name': 'Мастер', 'prop': 'box', 'locked': True}]}], 'guests': {}}
        got = server.tidy(self.folder, {'k:b': '', 'k:c': ''}, raw_office([('Код', [desk('k:b', 'Сборщик'), desk('k:c', 'Чистильщик')])]), old)
        self.assertEqual(self.seats(got)['k:b'], 'МОЁ')
        self.assertEqual([d['name'] for z in got['zones'] for d in z['desks'] if d['type'] == 'k:b'], ['Мастер'])

    def test_adding_moves_nobody(self):
        old = {'title': 'ШТАБ', 'zones': [{'id': 'код', 'name': 'КОД', 'desks': [{'type': 'main', 'name': 'Тимлид', 'prop': 'crown'}, {'type': 'k:b', 'name': 'Правщик', 'prop': 'box'}]}], 'guests': {}}
        raw = raw_office([('Проверка', [desk('k:b', 'Другой'), desk('k:r', 'Ревьюер')])], boss='Босс', title='НОВОЕ')
        got = server.tidy(self.folder, {'k:b': '', 'k:r': ''}, raw, old, mode='add')
        self.assertEqual(self.seats(got), {'main': 'КОД', 'k:b': 'КОД', 'k:r': 'ПРОВЕРКА'})
        self.assertEqual(got['title'], 'ШТАБ')
        self.assertEqual([d['name'] for d in got['zones'][0]['desks']], ['Тимлид', 'Правщик'])

    def test_common_tools_sit_apart_from_the_team_of_the_project(self):
        files = {'p:a': {'role': 'Скаут', 'scope': 'project', 'plugin': 'p'}, 'c:b': {'role': '', 'scope': 'user', 'plugin': 'c'}}
        got = server.tidy(self.folder, {'p:a': '', 'c:b': ''}, raw_office([('Работа', [desk('p:a', 'Скаут'), desk('c:b', 'Сборщик')])]), None, files)
        self.assertEqual(self.seats(got)['c:b'], 'ПОДДЕРЖКА')
        self.assertEqual(got['zones'][-1]['name'], 'ПОДДЕРЖКА')

    def test_without_a_team_common_tools_are_the_team(self):
        files = {'c:b': {'role': '', 'scope': 'user', 'plugin': 'c'}}
        got = server.tidy(self.folder, {'c:b': ''}, raw_office([('Работа', [desk('c:b', 'Сборщик')])]), None, files)
        self.assertEqual(self.seats(got)['c:b'], 'РАБОТА')

    def test_lead_skill_names_the_boss(self):
        table = 'Вызывает `scout`, `editor`, `critic`.'
        self.team.plugin('p', {'scout': ('Скаут', ''), 'editor': ('Монтажёр', ''), 'critic': ('Критик', '')}, scope='project', skill='# Продюсер\n\n' + table)
        lead = server.find_lead(self.folder, server.agent_files(self.folder))
        self.assertEqual(lead['role'], 'Продюсер')
        self.assertIn(table, lead['text'])
        got = server.tidy(self.folder, {'p:scout': ''}, raw_office([('Работа', [desk('p:scout', 'Скаут')])]), None, server.agent_files(self.folder), lead=lead)
        self.assertEqual(got['zones'][0]['desks'][0]['name'], 'Продюсер')

    def test_language_of_the_office_decides_what_is_a_foreign_name(self):
        got = server.tidy(self.folder, {'k:b': ''}, raw_office([('Build', [desk('k:b', 'Builder')])], boss='Lead', title='Crew'), {'lang': 'en', 'zones': [], 'guests': {}})
        self.assertEqual((got['lang'], got['title'], got['zones'][0]['name']), ('en', 'CREW', 'BUILD'))
        self.assertEqual([d['name'] for d in got['zones'][0]['desks']], ['Lead', 'Builder'])

    def test_foreign_title_from_the_model_keeps_the_old_sign(self):
        got = server.tidy(self.folder, {'k:b': ''}, raw_office([('Код', [desk('k:b', 'Сборщик')])], title='Smart TV'), {'title': 'ТЕЛЕВИЗОР', 'zones': [], 'guests': {}})
        self.assertEqual(got['title'], 'ТЕЛЕВИЗОР')

    def test_changes_are_counted(self):
        old = {'zones': [{'name': 'А', 'desks': [{'type': 'x', 'name': 'Икс'}, {'type': 'y', 'name': 'Игрек'}]}]}
        new = {'zones': [{'name': 'А', 'desks': [{'type': 'x', 'name': 'Икс 2'}]}, {'name': 'Б', 'desks': [{'type': 'y', 'name': 'Игрек'}, {'type': 'z', 'name': 'Зет'}]}]}
        self.assertEqual(server.changes(old, new), {'moved': 1, 'renamed': 1, 'added': 1})

    def test_old_file_gets_ids_and_language_on_reading(self):
        os.makedirs(os.path.dirname(server.office_path(self.folder)))
        with open(server.office_path(self.folder), 'w') as f:
            json.dump({'folder': self.folder, 'title': 'Т', 'zones': [{'name': 'РАЗВЕДКА', 'desks': []}], 'guests': {}}, f)
        office = server.load_office(self.folder)
        self.assertEqual((office['v'], office['lang'], office['zones'][0]['id']), (2, 'ru', 'разведка'))

    def test_manifest_of_the_project_decides(self):
        with open(os.path.join(self.folder, '.claude', 'office.json'), 'w') as f:
            json.dump({'title': 'Студия', 'boss': {'name': 'Шеф'}, 'zones': [{'name': 'Съёмка', 'desks': [{'type': 'k:c', 'name': 'Оператор'}]}]}, f)
        old = server.apply_manifest(None, self.folder)
        got = server.tidy(self.folder, {'k:c': '', 'k:d': ''}, raw_office([('Прочее', [desk('k:c', 'Камера'), desk('k:d', 'Дизайнер')])]), old)
        self.assertEqual(got['zones'][0]['name'], 'СЪЁМКА')
        self.assertEqual(got['title'], 'СТУДИЯ')
        self.assertEqual([d['name'] for d in got['zones'][0]['desks']], ['Шеф', 'Оператор'])

    def test_prompt_carries_instructions_usage_and_current_office(self):
        self.team.plugin('p', {'scout': ('Скаут', 'план.')}, scope='project')
        pdir, _ = self.team.session('s', {'p:scout': 'план.'})
        self.team.called(pdir, 's', ['p:scout'])
        with open(os.path.join(self.folder, 'CLAUDE.md'), 'w') as f:
            f.write('Заметка про кошек.\nАгенты работают конвейером: сначала скаут.\n')
        roster, files = server.staff_of(self.folder, {'p:scout': 'план.'})
        old = {'title': 'ШТАБ', 'zones': [{'id': 'поиск', 'name': 'ПОИСК', 'desks': [{'type': 'p:scout', 'name': 'Скаут', 'prop': 'box'}]}], 'guests': {}}
        text = server.office_prompt(self.folder, roster, files, old, 'full')
        self.assertIn('Находит лучшие куски', text)
        self.assertIn('вызовов: 1', text)
        self.assertIn('Агенты работают конвейером', text)
        self.assertNotIn('кошек', text)
        self.assertIn('ПОИСК', text)

    def test_prompt_for_adding_names_only_the_newcomers(self):
        old = {'title': 'ШТАБ', 'pending': ['k:n'], 'zones': [], 'guests': {}}
        text = server.office_prompt(self.folder, {'k:n': ''}, {}, old, 'add')
        self.assertIn('Новые агенты: k:n', text)


class AutoStaffTest(TeamCase):
    def setUp(self):
        super().setUp()
        self.find, self.generated = server.find_claude, []
        self.start = server.start_office
        server.start_office = lambda folder, roster, mode='full': self.generated.append((folder, mode)) or {}

    def tearDown(self):
        server.find_claude, server.start_office = self.find, self.start
        super().tearDown()

    def saved(self, **more):
        server.write_office(self.folder, dict({'folder': self.folder, 'title': 'ШТАБ', 'v': 2, 'lang': 'ru', 'guests': {}, 'zones': [
            {'id': 'код', 'name': 'КОД', 'desks': [{'type': 'main', 'name': 'Тимлид', 'prop': 'crown'}, {'type': 'p:a', 'name': 'Скаут', 'prop': 'box'}]}]}, **more))

    def test_new_agent_gets_a_desk_at_once_and_a_generation(self):
        self.saved()
        self.team.session('s', {'p:a': '', 'p:reviewer': 'проверяет'})
        server.watch_staff()
        office = server.load_office(self.folder)
        self.assertEqual(self.seats(office)['p:reviewer'], 'КОД')  # next to the colleagues from the same plugin
        self.assertEqual(office['pending'], ['p:reviewer'])
        self.assertEqual(self.generated, [(self.folder, 'add')])

    def test_agent_dropped_in_as_a_file_is_noticed_without_a_session(self):
        self.saved()
        self.team.session('s', {'p:a': ''})
        self.team.plugin('p', {'a': ('Скаут', ''), 'fresh': ('Новичок', 'x')}, scope='project')
        server.watch_staff()
        self.assertEqual(self.seats(server.load_office(self.folder))['p:fresh'], 'КОД')

    def test_nothing_new_nothing_spent(self):
        self.saved()
        self.team.session('s', {'p:a': '', 'Explore': ''})
        server.watch_staff()
        self.assertEqual(self.generated, [])

    def test_folder_without_an_office_is_not_generated_on_its_own(self):
        self.team.session('s', {'p:a': ''})
        server.watch_staff()
        self.assertEqual(self.generated, [])
        self.assertIsNone(server.load_office(self.folder))

    def test_failed_generation_is_not_repeated_every_pass(self):
        self.saved()
        self.team.session('s', {'p:a': '', 'p:b': ''})
        server.watch_staff()
        server.watch_staff()
        server.watch_staff()
        self.assertEqual(len(self.generated), 1)

    def test_office_can_opt_out(self):
        self.saved(auto=False)
        self.team.session('s', {'p:a': '', 'p:b': ''})
        server.watch_staff()
        self.assertEqual(self.generated, [])
        self.assertNotIn('p:b', self.seats(server.load_office(self.folder)))

    def test_old_session_does_not_touch_a_bigger_office(self):
        self.saved()
        self.team.session('old', {'kit:x': ''}, age=4000)
        self.team.session('new', {'p:a': ''}, age=5)
        server.watch_staff()
        self.assertNotIn('kit:x', self.seats(server.load_office(self.folder)))


class SessionViewTest(TeamCase):
    def office(self):
        server.write_office(self.folder, {'folder': self.folder, 'title': 'ШТАБ', 'v': 2, 'lang': 'ru', 'guests': {}, 'zones': [
            {'id': 'а', 'name': 'А', 'desks': [{'type': 'main', 'name': 'Босс', 'prop': 'crown'}, {'type': 'k:a', 'name': 'Первый', 'prop': 'box'}]},
            {'id': 'б', 'name': 'Б', 'desks': [{'type': 'k:b', 'name': 'Второй', 'prop': 'box'}]}]})

    def test_session_shows_only_who_it_can_call(self):
        self.office()
        _, path = self.team.session('s', {'k:a': ''})
        got = server.office_for(server.tail(path))
        self.assertEqual(self.seats(got), {'main': 'А', 'k:a': 'А'})
        self.assertEqual(got['hidden'], 1)

    def test_agent_that_really_worked_keeps_the_desk(self):
        self.office()
        _, path = self.team.session('s', {'k:a': ''})
        self.assertIn('k:b', self.seats(server.office_for(server.tail(path), ['k:b'])))

    def test_session_without_a_list_shows_the_whole_office(self):
        self.office()
        _, path = self.team.session('s', {})
        self.assertEqual(len(self.seats(server.office_for(server.tail(path)))), 3)


class ClaudeEnvTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        self.scutil = server.system_proxy
        for key in list(os.environ):
            if key.lower().endswith('_proxy'):
                del os.environ[key]
        server.system_proxy = lambda: {}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        server.system_proxy = self.scutil
        self.dir.cleanup()

    def shell(self, body):
        path = os.path.join(self.dir.name, 'sh')
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(path, 0o755)
        os.environ['SHELL'] = path

    def test_proxy_comes_from_the_users_shell(self):
        self.shell('echo HTTPS_PROXY=http://proxy.test:1\necho OTHER=1\n')
        env = server.claude_env('/bin/claude')
        self.assertEqual(env['HTTPS_PROXY'], 'http://proxy.test:1')
        self.assertNotIn('OTHER', env)
        self.assertTrue(env['PATH'].startswith('/bin:'))

    def test_proxy_already_set_wins(self):
        os.environ['HTTPS_PROXY'] = 'http://own.test:2'
        self.shell('echo HTTPS_PROXY=http://proxy.test:1\n')
        self.assertEqual(server.claude_env('/bin/claude')['HTTPS_PROXY'], 'http://own.test:2')

    def test_system_proxy_when_the_shell_has_none(self):
        self.shell('echo OTHER=1\n')
        server.system_proxy = lambda: {'HTTPS_PROXY': 'http://system.test:3'}
        self.assertEqual(server.claude_env('/bin/claude')['HTTPS_PROXY'], 'http://system.test:3')

    def test_broken_shell_is_not_fatal(self):
        os.environ['SHELL'] = os.path.join(self.dir.name, 'нет')
        self.assertNotIn('HTTPS_PROXY', server.claude_env('/bin/claude'))

    def test_scutil_answer_is_read(self):
        text = """<dictionary> {
  HTTPEnable : 1
  HTTPPort : 7890
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 7891
  HTTPSProxy : 127.0.0.1
}"""
        self.assertEqual(server.read_scutil(text), {'HTTP_PROXY': 'http://127.0.0.1:7890', 'HTTPS_PROXY': 'http://127.0.0.1:7891'})

    def test_scutil_without_proxy_gives_nothing(self):
        self.assertEqual(server.read_scutil('<dictionary> {\n  HTTPEnable : 0\n}'), {})


class WatchParentTest(unittest.TestCase):
    def test_parent_alive_false_when_ppid_differs(self):
        self.assertTrue(server.parent_alive(os.getppid()))
        self.assertFalse(server.parent_alive(os.getppid() + 12345))


class OfficeTest(unittest.TestCase):
    roster = {'kit:reviewer': '', 'kit:builder': '', 'solo': '', 'Explore': ''}

    def test_russian_job_titles(self):
        self.assertEqual(server.ru_name('caveman:cavecrew-reviewer'), 'Ревьюер')
        self.assertEqual(server.ru_name('codex:codex-rescue'), 'Спасатель')
        self.assertEqual(server.ru_name('reels-core:motion-designer'), 'Аниматор')
        self.assertEqual(server.ru_name('reels-core:qa-inspector'), 'Контролёр')
        self.assertEqual(server.ru_name('x:art-director'), 'Арт-директор')
        self.assertEqual(server.ru_name('x:layer-inspector'), 'Инспектор')  # head noun when the phrase is unknown
        self.assertEqual(server.ru_name('solo'), 'Solo')  # nothing to translate: the name stays

    def test_auto_office_groups_by_kind_of_work_in_russian(self):
        roster = {'p:scout': '', 'p:reviewer': '', 'p:builder': '', 'q:codex-rescue': '', 'q:scriptwriter': '', 'solo': '', 'Explore': ''}
        office = server.auto_office('/work/app', roster)
        zones = {z['name']: [d['name'] for d in z['desks']] for z in office['zones']}
        self.assertEqual(zones, {'РАЗВЕДКА': ['Главный', 'Скаут'], 'ПЛАН': ['Сценарист'], 'ПРОИЗВОДСТВО': ['Сборщик'],
                                 'КАЧЕСТВО': ['Ревьюер'], 'ПОДДЕРЖКА': ['Спасатель'], 'ПРОЧИЕ': ['Solo']})
        self.assertEqual([z['name'] for z in office['zones']], ['РАЗВЕДКА', 'ПЛАН', 'ПРОИЗВОДСТВО', 'КАЧЕСТВО', 'ПОДДЕРЖКА', 'ПРОЧИЕ'])
        self.assertTrue(all(d['prop'] in server.PROPS for z in office['zones'] for d in z['desks']))
        self.assertEqual(office['title'], 'ОФИС · APP')

    def test_auto_office_seats_builtins_as_guests(self):
        office = server.auto_office('/work/app', {'p:scout': '', 'Explore': '', 'general-purpose': '', 'claude-code-guide': ''})
        self.assertEqual(office['guests'], {'Explore': {'name': 'Разведчик', 'prop': 'binoculars'},
                                            'general-purpose': {'name': 'Универсал', 'prop': 'layers'},
                                            'claude-code-guide': {'name': 'Проводник', 'prop': 'books'}})
        self.assertNotIn('Explore', [d['type'] for z in office['zones'] for d in z['desks']])

    def test_session_view_keeps_guests_this_session_can_call(self):
        saved = {'lang': 'ru', 'zones': [{'name': 'КОД', 'desks': [{'type': 'k:b', 'name': 'Правщик'}]}],
                 'guests': {'Explore': {'name': 'Следопыт', 'prop': 'binoculars'}, 'Plan': {'name': 'Архитектор', 'prop': 'chart'}}}
        got = server.session_view(saved, {'k:b': '', 'Explore': '', 'claude': ''}, called=['general-purpose'])
        # the saved name stays; Plan is not in this session; a new built-in gets a name without the model
        self.assertEqual(got['guests'], {'Explore': {'name': 'Следопыт', 'prop': 'binoculars'},
                                         'claude': {'name': 'Помощник', 'prop': 'tablet'},
                                         'general-purpose': {'name': 'Универсал', 'prop': 'layers'}})

    def test_model_answer_is_tidied(self):
        raw = {
            'title': 'Мастерская',
            'boss': {'name': 'Тимлид', 'prop': 'crown'},
            'zones': [{'name': 'код', 'desks': [
                {'type': 'kit:builder', 'name': 'Сборщик', 'prop': 'box'},
                {'type': 'made:up', 'name': 'Призрак', 'prop': 'cap'},
                {'type': 'kit:builder', 'name': 'Дубль', 'prop': 'box'},
            ]}],
            # the model sat a plugin agent among the guests: it gets a desk, keeping its Russian title
            'guests': [{'type': 'Explore', 'name': 'Разведчик', 'prop': 'binoculars'},
                       {'type': 'kit:reviewer', 'name': 'Проверяльщик', 'prop': 'glasses'}],
        }
        office = server.tidy('/work/app', self.roster, raw)
        zones = {z['name']: [d['name'] for d in z['desks']] for z in office['zones']}
        self.assertEqual(zones, {'КОД': ['Тимлид', 'Сборщик'], 'ПРОЧИЕ': ['Проверяльщик', 'Solo']})
        self.assertEqual(office['guests'], {'Explore': {'name': 'Разведчик', 'prop': 'binoculars'}})
        self.assertEqual(office['title'], 'МАСТЕРСКАЯ')

    def test_latin_titles_from_the_model_are_replaced(self):
        raw = {'title': 'Офис', 'boss': {'name': 'Boss', 'prop': 'crown'},
               'zones': [{'name': 'Build', 'desks': [{'type': 'kit:builder', 'name': 'Builder', 'prop': 'box'}]}], 'guests': []}
        office = server.tidy('/work/app', {'kit:builder': ''}, raw)
        self.assertEqual([d['name'] for d in office['zones'][0]['desks']], ['Главный', 'Сборщик'])
        self.assertEqual(office['zones'][0]['name'], 'ПРОИЗВОДСТВО')


class ShotTest(unittest.TestCase):
    png = b'\x89PNG\r\n\x1a\n' + b'pixels'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, 'Agent Office')
        shots, server.SHOTS = server.SHOTS, self.dir
        self.addCleanup(setattr, server, 'SHOTS', shots)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def post(self, body, headers=None, path='/api/shot'):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        self.addCleanup(conn.close)
        conn.request('POST', path, body, {'Content-Type': 'image/png'} if headers is None else headers)
        res = conn.getresponse()
        return res.status, json.loads(res.read())

    def test_saves_png_into_its_folder(self):
        status, body = self.post(self.png)
        self.assertEqual(status, 201)
        self.assertEqual(os.path.dirname(body['path']), self.dir)
        self.assertRegex(os.path.basename(body['path']), r'^agent-office \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.png$')
        with open(body['path'], 'rb') as f:
            self.assertEqual(f.read(), self.png)

    def test_two_shots_in_one_second_keep_both_files(self):
        with mock.patch.object(server.time, 'strftime', return_value='2026-10-30 12-00-00'):
            first = server.save_shot(self.png, 'Все на кофе')
            second = server.save_shot(self.png, 'Все на кофе')
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(os.listdir(self.dir)), ['Все на кофе 2026-10-30 12-00-00-2.png', 'Все на кофе 2026-10-30 12-00-00.png'])

    def test_funny_name_goes_into_the_file_name(self):
        status, body = self.post(self.png, path='/api/shot?name=' + quote('Все на кофе'))
        self.assertEqual(status, 201)
        self.assertTrue(os.path.basename(body['path']).startswith('Все на кофе 20'))

    def test_name_cannot_leave_the_folder(self):
        status, body = self.post(self.png, path='/api/shot?name=' + quote('../../.ssh/evil\0.png'))
        self.assertEqual(status, 201)
        self.assertEqual(os.path.dirname(body['path']), self.dir)
        self.assertTrue(os.path.basename(body['path']).startswith('ssh'))

    def test_empty_name_falls_back(self):
        self.assertEqual(server.shot_name('/../..'), 'agent-office')
        self.assertLessEqual(len(server.shot_name('я' * 300)), 80)

    def test_refuses_what_is_not_a_png(self):
        status, _ = self.post(b'<script>')
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(self.dir))

    def test_refuses_form_posts_from_other_sites(self):
        # text/plain уходит с чужой страницы без preflight, image/png — нет
        status, _ = self.post(self.png, {'Content-Type': 'text/plain'})
        self.assertEqual(status, 415)
        self.assertFalse(os.path.exists(self.dir))

    def test_refuses_oversized_body_without_reading_it(self):
        with mock.patch.object(server, 'SHOT_LIMIT', 4):
            status, _ = self.post(self.png)
        self.assertEqual(status, 413)
        self.assertFalse(os.path.exists(self.dir))

    def test_unwritable_folder_is_an_error_not_a_crash(self):
        with open(os.path.join(os.path.dirname(self.dir), 'blocker'), 'w'):
            pass
        server.SHOTS = os.path.join(os.path.dirname(self.dir), 'blocker', 'shots')
        status, body = self.post(self.png)
        self.assertEqual(status, 500)
        self.assertIn('error', body)

    def test_negative_length_is_refused(self):
        # rfile.read(-1) читал бы, пока клиент не закроет соединение
        with socket.create_connection(('127.0.0.1', self.http.server_port), timeout=2) as s:
            s.sendall(b'POST /api/shot HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: image/png\r\nContent-Length: -1\r\n\r\n')
            self.assertIn(b' 400 ', s.recv(200))

    def test_stalled_upload_is_dropped(self):
        self.assertGreater(server.Handler.timeout or 0, 0)
        with mock.patch.object(server.Handler, 'timeout', 0.2), socket.create_connection(('127.0.0.1', self.http.server_port), timeout=3) as s:
            s.sendall(b'POST /api/shot HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: image/png\r\nContent-Length: 10\r\n\r\n')
            while s.recv(200):  # что бы сервер ни ответил, соединение он закрывает сам, клиента не ждёт
                pass

    def test_failed_post_answers_with_json(self):
        with mock.patch.object(server, 'find_tail', side_effect=RuntimeError('сбой')):
            status, body = self.post(b'', {}, '/api/office?s=x')
        self.assertEqual(status, 500)
        self.assertIn('error', body)


class ForeignRequestTest(unittest.TestCase):
    """Чужой сайт не должен достучаться до офиса: ни через свой домен на 127.0.0.1, ни запросом со своей страницы."""
    png = b'\x89PNG\r\n\x1a\n' + b'pixels'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.shots = os.path.join(tmp.name, 'shots')
        for name, value in (('SHOTS', self.shots), ('PROJECTS', tmp.name), ('CODEX_SESSIONS', os.path.join(tmp.name, 'нет'))):
            self.addCleanup(setattr, server, name, getattr(server, name))
            setattr(server, name, value)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.port = self.http.server_port

    def ask(self, method, path, headers, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        self.addCleanup(conn.close)
        conn.request(method, path, body, headers)
        res = conn.getresponse()
        res.read()
        return res.status

    def shot(self, **headers):
        return self.ask('POST', '/api/shot', dict({'Content-Type': 'image/png'}, **headers), self.png)

    def test_foreign_host_reads_nothing(self):
        for path in ('/api/sessions', '/api/stats', '/api/state?s=x', '/'):
            self.assertEqual(self.ask('GET', path, {'Host': 'evil.example:%d' % self.port}), 403, path)

    def test_foreign_host_writes_nothing(self):
        self.assertEqual(self.shot(Host='evil.example'), 403)
        self.assertFalse(os.path.exists(self.shots))

    def test_foreign_origin_writes_nothing(self):
        self.assertEqual(self.shot(Origin='https://evil.example'), 403)
        self.assertEqual(self.shot(Origin='null'), 403)
        self.assertFalse(os.path.exists(self.shots))

    def test_own_page_works(self):
        for host in ('localhost', '127.0.0.1'):
            own = '%s:%d' % (host, self.port)
            self.assertEqual(self.ask('GET', '/api/sessions', {'Host': own}), 200)
            self.assertEqual(self.shot(Host=own, Origin='http://' + own), 201)

    def test_server_opened_to_the_network_takes_any_name(self):
        with mock.patch.object(server, 'HOSTS', server.trusted_hosts('0.0.0.0')):
            self.assertEqual(self.ask('GET', '/api/sessions', {'Host': '192.168.1.5:%d' % self.port}), 200)

    def test_named_address_is_trusted(self):
        hosts = server.trusted_hosts('192.168.1.5')
        self.assertIn('192.168.1.5', hosts)
        self.assertIn('localhost', hosts)
        self.assertNotIn('evil.example', hosts)


class BrokenJournalTest(unittest.TestCase):
    """Журналы пишет другая программа: странная запись теряется сама, а не вместе со всем списком."""
    roster_line = line(type='attachment', attachment={'type': 'agent_listing_delta', 'addedTypes': ['b']})

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, 's.jsonl')

    def write(self, text):
        with open(self.path, 'w') as f:
            f.write(text)

    def ledger(self):
        return server.Ledger(os.path.join(self.dir.name, 'stats.json'))

    def odd(self, **message):
        return line(type='assistant', timestamp='2026-01-01T10:00:00Z', message=dict({
            'id': 'odd', 'model': 'claude-sonnet-5-5', 'usage': {'input_tokens': 1}, 'content': []}, **message))

    def test_tail_skips_a_line_it_cannot_take(self):
        self.write(self.odd(model=['x']) + self.odd(id={}) + assistant('m1', [('Read', {'file_path': 'a'})]))
        self.assertEqual(server.Tail(self.path).feed().calls, 1)

    def test_ledger_skips_a_line_it_cannot_take(self):
        self.write(self.odd(model=['x']) + assistant('m1'))
        ledger = self.ledger()
        ledger.feed(self.path, True)
        self.assertEqual([list(models) for hour in ledger.hours.values() for models in hour.values()], [['claude-sonnet-5-5']])

    def test_codex_ledger_skips_a_line_it_cannot_take(self):
        self.write(meta() + context(model=['x']) + usage_record() + rec('token_usage_record', {'usage': {'input_tokens': None}})
                   + context() + usage_record())
        ledger = self.ledger()
        ledger.feed_codex(self.path, self.path)
        self.assertEqual([list(models) for hour in ledger.hours.values() for models in hour.values()], [['gpt-5.6-sol']])

    def test_roster_skips_a_line_it_cannot_take(self):
        self.write(self.roster_line + line(type='attachment', attachment={'type': 'agent_listing_delta', 'removedTypes': [[]]}))
        self.assertEqual(list(server.roster_in(self.path)), ['b'])

    def test_shortened_journal_is_not_counted_twice(self):
        # id ответов ledger помнит не все: перечитать журнал с начала значит удвоить траты
        ledger = self.ledger()
        self.write(''.join(assistant('m%d' % i) for i in range(ledger.IDS + 1)) + line(type='progress'))
        ledger.feed(self.path, True)
        before = json.dumps(ledger.hours, sort_keys=True)
        self.write(''.join(assistant('m%d' % i) for i in range(ledger.IDS + 1)))
        ledger.feed(self.path, True)
        self.assertEqual(json.dumps(ledger.hours, sort_keys=True), before)

    def test_unfinished_line_waits_for_its_end(self):
        whole = assistant('m1', [('Read', {'file_path': 'a'})])
        self.write(whole[:20])
        t = server.Tail(self.path).feed()
        self.assertEqual(t.calls, 0)
        self.write(whole)
        self.assertEqual(t.feed().calls, 1)

    def peak_of(self, read):
        noise = line(type='progress', data='x' * 1000)
        self.write(noise * 20000)
        tracemalloc.start()
        try:
            read()
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_tail_does_not_load_a_huge_journal_at_once(self):
        self.assertLess(self.peak_of(lambda: server.Tail(self.path).feed()), 2_000_000)

    def test_ledger_does_not_load_a_huge_journal_at_once(self):
        self.assertLess(self.peak_of(lambda: self.ledger().feed(self.path, True)), 2_000_000)

    def test_codex_titles_skip_bad_rows(self):
        self.write('[1, 2]\nне json\n{"id": ["x"]}\n{"id": "a", "thread_name": "Имя"}\n')
        with mock.patch.object(server, 'CODEX_INDEX', self.path), mock.patch.dict(server.CODEX_TITLES, mtime=0, names={}):
            self.assertEqual(server.codex_titles(), {'a': 'Имя'})


class VanishingJournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(server.TAILS.clear)
        for name, value in (('PROJECTS', self.dir.name), ('CODEX_SESSIONS', os.path.join(self.dir.name, 'нет'))):
            self.addCleanup(setattr, server, name, getattr(server, name))
            setattr(server, name, value)
        server.TAILS.clear()
        self.proj = os.path.join(self.dir.name, 'proj')
        self.sub = os.path.join(self.proj, 'live', 'subagents')
        os.makedirs(self.sub)
        for path in (os.path.join(self.proj, 'live.jsonl'), os.path.join(self.proj, 'gone.jsonl'), os.path.join(self.sub, 'agent-gone.jsonl')):
            with open(path, 'w') as f:
                f.write(assistant('m1', ts=iso(time.time())))

    def test_list_survives_a_journal_deleted_during_the_scan(self):
        real = os.path.getmtime

        def getmtime(path):
            if 'gone' in os.path.basename(str(path)):
                raise FileNotFoundError(path)
            return real(path)
        with mock.patch.object(server.os.path, 'getmtime', getmtime):
            self.assertEqual([s['id'] for s in server.sessions()], ['live'])


class GlobPathTest(unittest.TestCase):
    """Скобка в пути проекта не должна превращать путь в шаблон glob."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_agents_of_a_folder_with_brackets(self):
        folder = os.path.join(self.dir.name, 'проект [2]')
        os.makedirs(os.path.join(folder, '.claude', 'agents'))
        with open(os.path.join(folder, '.claude', 'agents', 'builder.md'), 'w') as f:
            f.write('# Сборщик\n')
        self.assertIn('builder', server.agent_files(folder))

    def test_journals_under_a_config_folder_with_brackets(self):
        root = os.path.join(self.dir.name, 'конфиг [2]')
        sub = os.path.join(root, '-work-app', 's', 'subagents')
        os.makedirs(sub)
        with open(os.path.join(sub, 'agent-a.meta.json'), 'w') as f:
            json.dump({'agentType': 'kit:builder'}, f)
        with mock.patch.object(server, 'PROJECTS', root):
            self.assertEqual(server.journal_dirs('/work/app'), [os.path.join(root, '-work-app')])
            self.assertEqual(server.usage_of('/work/app')[0], {'kit:builder': 1})


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(setattr, server, 'DATA', server.DATA)
        server.DATA = self.dir.name

    def test_office_writes_wait_for_each_other(self):
        entered, release, calls = threading.Event(), threading.Event(), []
        real = json.dump

        def slow(obj, f, **kw):
            calls.append(obj)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
            real(obj, f, **kw)
        with mock.patch.object(server.json, 'dump', slow):
            first = threading.Thread(target=server.write_office, args=('/work/app', {'n': 1}))
            second = threading.Thread(target=server.write_office, args=('/work/app', {'n': 2}))
            first.start()
            entered.wait(5)
            second.start()
            second.join(0.3)
            overlapped = not second.is_alive()
            release.set()
            first.join()
            second.join()
        self.assertFalse(overlapped)
        self.assertEqual(server.load_office('/work/app')['n'], 2)

    def test_two_starts_make_one_job(self):
        class Slow(dict):
            def get(self, *a):
                value = super().get(*a)
                time.sleep(0.05)
                return value
        started = []
        with mock.patch.object(server, 'JOBS', Slow()), mock.patch.object(server, 'run_office'):
            threads = [threading.Thread(target=lambda: started.append(server.start_office('/work/app', {}))) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sum(job is not None for job in started), 1)


class SignalTest(unittest.TestCase):
    def test_ctrl_c_stops_claude_too(self):
        # claude живёт в своей сессии: Ctrl-C до него не дойдёт, погасить его должен сервер
        with mock.patch.object(sys, 'argv', ['server.py']), mock.patch.object(server, 'ThreadingHTTPServer'), \
                mock.patch.object(server.threading, 'Thread'), mock.patch.object(server.signal, 'signal') as caught, \
                mock.patch('builtins.print'):
            server.main()
        self.assertIn(mock.call(signal.SIGINT, server.on_term), caught.call_args_list)


class BackgroundShellTest(unittest.TestCase):
    """Серверная: фоновые команды руководителя, их время и итог."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.projects, server.PROJECTS = server.PROJECTS, os.path.join(self.dir.name, 'projects')
        self.live, server.SESSIONS_DIR = server.SESSIONS_DIR, os.path.join(self.dir.name, 'sessions')
        self.codex, server.CODEX_SESSIONS = server.CODEX_SESSIONS, os.path.join(self.dir.name, 'нет-codex')
        os.makedirs(server.SESSIONS_DIR)
        server.TAILS.clear()
        self.now = time.time()
        self.pdir = os.path.join(server.PROJECTS, '-work-app')
        os.makedirs(self.pdir)

    def tearDown(self):
        server.PROJECTS, server.SESSIONS_DIR, server.CODEX_SESSIONS = self.projects, self.live, self.codex
        server.TAILS.clear()
        self.dir.cleanup()

    def ago(self, seconds):
        return iso(self.now - seconds)

    def asked(self, age):
        return line(type='user', timestamp=self.ago(age), cwd='/work/app', message={'role': 'user', 'content': 'собери рилс'})

    def called(self, tool_id, age, description='Сборка рилса', name='Bash', **more):
        return line(type='assistant', timestamp=self.ago(age), cwd='/work/app', message={
            'id': 'm' + tool_id, 'model': 'claude-sonnet-5-5', 'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'id': tool_id, 'name': name, 'input': {'command': 'make', 'description': description, **more}}],
            'usage': {'input_tokens': 10, 'output_tokens': 1}})

    def in_background(self, tool_id, task, age):
        return line(type='user', timestamp=self.ago(age), cwd='/work/app', toolUseResult={'backgroundTaskId': task}, message={
            'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tool_id, 'content': 'moved to the background'}]})

    def finished_now(self, tool_id, age):
        return line(type='user', timestamp=self.ago(age), cwd='/work/app', toolUseResult={'stdout': 'ok'}, message={
            'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tool_id, 'content': 'ok'}]})

    def note(self, task, status='completed'):
        return '<task-notification>\n<task-id>%s</task-id>\n<status>%s</status>\n<summary>итог</summary>\n</task-notification>' % (task, status)

    def queued(self, task, age, status='completed'):
        return line(type='queue-operation', operation='enqueue', timestamp=self.ago(age), content=self.note(task, status))

    def told(self, task, age, status='completed'):
        return line(type='user', timestamp=self.ago(age), cwd='/work/app', isMeta=True, message={'role': 'user', 'content': self.note(task, status)})

    def killed(self, task, age, result=None):
        result = {'message': 'Successfully stopped task: %s (make)' % task, 'task_id': task, 'task_type': 'local_bash', 'command': 'make'} if result is None else result
        return line(type='user', timestamp=self.ago(age), cwd='/work/app', toolUseResult=result, message={
            'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'stop-' + task, 'content': 'stopped'}]})

    def stopped(self, age):
        return line(type='assistant', timestamp=self.ago(age), cwd='/work/app', message={
            'id': 'end', 'model': 'claude-sonnet-5-5', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'жду'}],
            'usage': {'input_tokens': 10, 'output_tokens': 1}})

    def journal(self, sid, *rows, age=0):
        path = os.path.join(self.pdir, sid + '.jsonl')
        with open(path, 'w') as f:
            f.writelines(rows)
        os.utime(path, (self.now - age, self.now - age))

    def shells(self, sid='s1'):
        return server.state(sid)['shells']

    def worked(self, sid='s1'):
        return server.state(sid)['worked']

    def agent(self, sid, name, *rows, finished=True):
        sub = os.path.join(self.pdir, sid, 'subagents')
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, 'agent-%s.jsonl' % name), 'w') as f:
            f.writelines(rows)
        with open(os.path.join(sub, 'agent-%s.meta.json' % name), 'w') as f:
            json.dump({'agentType': 'Explore', 'description': 'ищет', 'finished': finished}, f)

    def test_work_time_joins_head_agents_and_background_commands(self):
        self.journal('s1',
                     self.asked(1000), self.stopped(900),
                     # the command outlives the turn that started it
                     self.asked(500), self.called('b1', 500), self.in_background('b1', 'bg1', 499), self.stopped(498),
                     self.queued('bg1', 400), self.told('bg1', 400))
        self.agent('s1', 'a1', self.asked(950), self.stopped(850))
        self.assertEqual(self.worked(), 250000)  # 1000→850 and 500→400; the agent's overlap counts once
        [s] = server.sessions()
        self.assertEqual(s['worked'], 250000)

    def test_agent_without_a_head_turn_adds_its_own_time(self):
        self.journal('s1', self.asked(1000), self.stopped(900))
        self.agent('s1', 'a1', self.asked(800), self.stopped(700))
        self.assertEqual(self.worked(), 200000)

    def test_running_background_command_counts_until_now(self):
        self.journal('s1', self.asked(300), self.called('b1', 300), self.in_background('b1', 'bg1', 299), self.stopped(298), age=298)
        st = server.state('s1')
        self.assertEqual((st['worked'], st['workingSince']), (0, server.ms(self.ago(300))))

    def test_state_does_not_change_while_time_just_passes(self):
        self.journal('s1', self.asked(300), self.called('b1', 300), self.in_background('b1', 'bg1', 299), self.stopped(298), age=298)
        first = json.dumps(server.state('s1'), sort_keys=True)
        with mock.patch.object(server.time, 'time', return_value=time.time() + 5):
            self.assertEqual(json.dumps(server.state('s1'), sort_keys=True), first)

    def process(self, sid, pid=None):
        path = os.path.join(server.SESSIONS_DIR, '%d.json' % (pid or os.getpid()))
        with open(path, 'w') as f:
            json.dump({'pid': pid or os.getpid(), 'sessionId': sid}, f)
        self.addCleanup(server.SEEN_ALIVE.clear)
        return path

    def at_work(self):
        self.journal('s1', self.asked(300), self.called('b1', 300), self.in_background('b1', 'bg1', 299),
                     self.asked(100), self.called('t1', 100))

    def test_closed_session_stops_where_it_was_seen_last(self):
        self.at_work()
        self.agent('s1', 'a1', self.asked(100), self.called('c1', 50), finished=False)
        path = self.process('s1')
        self.assertTrue(server.state('s1')['workingSince'])
        os.remove(path)
        closed = server.state('s1')
        self.assertEqual(closed['workingSince'], 0)
        self.assertAlmostEqual(closed['worked'], 300000, delta=3000)
        self.assertEqual([(s['status'], bool(s['end'])) for s in closed['shells']], [('killed', True)])
        with mock.patch.object(server.time, 'time', return_value=time.time() + 3600):
            self.assertEqual(server.state('s1')['worked'], closed['worked'])
            [s] = server.sessions()
            self.assertEqual((s['workingSince'], s['shells'], s['isLive']), (0, 0, False))

    def test_dead_process_counts_as_closed(self):
        self.at_work()
        path = self.process('s1')
        server.state('s1')
        with open(path, 'w') as f:
            json.dump({'pid': 2 ** 22 + 1, 'sessionId': 's1'}, f)
        self.assertEqual(server.state('s1')['workingSince'], 0)

    def test_session_reopened_after_closing_ticks_again(self):
        self.at_work()
        path = self.process('s1')
        server.state('s1')
        os.remove(path)
        server.state('s1')
        self.process('s1')
        with open(os.path.join(self.pdir, 's1.jsonl'), 'a') as f:
            f.write(self.asked(1))
        self.assertTrue(server.state('s1')['workingSince'])

    def test_half_written_process_file_does_not_close_anything(self):
        self.at_work()
        path = self.process('s1')
        server.state('s1')
        with open(path, 'w') as f:
            f.write('{"pid"')
        self.assertTrue(server.state('s1')['workingSince'])

    def test_session_never_seen_alive_keeps_its_clock(self):
        self.at_work()
        self.assertTrue(server.state('s1')['workingSince'])

    def test_lost_command_keeps_the_time_it_was_seen_running(self):
        self.journal('s1', self.asked(300), self.called('b1', 300), self.in_background('b1', 'bg1', 299), self.stopped(298))
        first = server.state('s1')
        self.assertTrue(first['workingSince'])
        later = time.time() + server.BG_LOST_S + 100
        with mock.patch.object(server.time, 'time', return_value=later):
            self.assertAlmostEqual(server.state('s1')['worked'], 300000, delta=3000)

    def test_only_an_agent_at_work_keeps_the_clock_ticking(self):
        self.journal('s1', self.asked(1000), self.stopped(900))
        self.agent('s1', 'a1', self.asked(100), self.called('c1', 50), finished=False)
        first = server.state('s1')
        self.assertEqual((first['worked'], first['workingSince']), (100000, server.ms(self.ago(100))))
        with mock.patch.object(server.time, 'time', return_value=time.time() + 5):
            self.assertEqual(json.dumps(server.state('s1'), sort_keys=True), json.dumps(first, sort_keys=True))

    def test_lost_background_command_stops_counting(self):
        age = server.BG_LOST_S + 100
        self.journal('s1', self.asked(age), self.called('b1', age), self.in_background('b1', 'bg1', age - 1), self.stopped(age - 2))
        self.assertLess(self.worked(), 10000)

    def test_lost_background_command_keeps_the_time_the_session_saw(self):
        age = server.BG_LOST_S + 2000
        self.journal('s1', self.asked(age), self.called('b1', age), self.in_background('b1', 'bg1', age - 1), self.stopped(age - 2),
                     self.asked(age - 1000), self.stopped(age - 1001))
        self.assertEqual(self.worked(), 1001000)

    def test_finished_command_counts_after_it_left_the_server_room(self):
        age = server.BG_DONE_S + 1000
        self.journal('s1', self.asked(age), self.called('b1', age), self.in_background('b1', 'bg1', age - 1), self.stopped(age - 2), self.queued('bg1', age - 500))
        self.assertEqual(self.shells(), [])
        self.assertEqual(self.worked(), 500000)

    def test_quiet_session_stops_counting_at_its_last_record(self):
        thinking = line(type='assistant', timestamp=self.ago(server.LIVE_S + 100), cwd='/work/app', message={
            'id': 'm', 'model': 'claude-sonnet-5-5', 'content': [{'type': 'text', 'text': 'думаю'}]})
        self.journal('s1', self.asked(server.LIVE_S + 200), thinking, age=server.LIVE_S + 100)
        self.assertEqual(self.worked(), 100000)

    def test_finished_agent_stops_counting_at_once(self):
        self.journal('s1', self.asked(1000), self.stopped(900))
        self.agent('s1', 'a1', self.asked(100), self.called('c1', 50))
        self.assertEqual(self.worked(), 150000)

    def test_agent_clock_details_stay_on_the_server(self):
        self.journal('s1', self.asked(1000), self.stopped(900))
        self.agent('s1', 'a1', self.asked(800), self.stopped(700))
        self.assertNotIn('spans', server.state('s1')['agents'][0])

    def test_running_command_counts_from_the_call_not_from_the_move_to_background(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 80))
        self.assertEqual(self.shells(), [{'id': 'b1', 'name': 'Сборка рилса', 'session': 1, 'status': 'running',
                                          'start': int((self.now - 200)) * 1000, 'end': 0}])

    def test_command_finished_in_the_foreground_is_not_shown(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.finished_now('t1', 190))
        self.assertEqual(self.shells(), [])

    def test_notification_in_the_queue_ends_the_command(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190), self.queued('b1', 20))
        got = self.shells()[0]
        self.assertEqual((got['status'], got['end']), ('completed', int(self.now - 20) * 1000))

    def test_notification_in_a_user_record_ends_the_command(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190), self.told('b1', 20, 'failed'))
        self.assertEqual(self.shells()[0]['status'], 'failed')

    def test_first_notification_keeps_the_end_time(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190),
                     self.queued('b1', 30), self.told('b1', 10))
        self.assertEqual(self.shells()[0]['end'], int(self.now - 30) * 1000)

    def test_quoted_notification_of_an_unknown_task_changes_nothing(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190), self.told('чужой', 20))
        self.assertEqual([s['status'] for s in self.shells()], ['running'])

    def test_stopped_command_is_killed_at_the_time_of_the_stop(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190), self.killed('b1', 20))
        got = self.shells()[0]
        self.assertEqual((got['status'], got['end']), ('killed', int(self.now - 20) * 1000))

    def test_session_cools_down_after_the_command_was_stopped(self):
        quiet = server.LIVE_S + 60
        self.journal('s1', self.asked(quiet + 50), self.called('t1', quiet + 20), self.in_background('t1', 'b1', quiet + 10), self.killed('b1', quiet), age=quiet)
        got = {s['id']: s for s in server.sessions()}['s1']
        self.assertEqual((got['isLive'], got['shells']), (False, 0))

    def test_stop_of_an_unknown_task_changes_nothing(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190), self.killed('чужой', 20))
        self.assertEqual([s['status'] for s in self.shells()], ['running'])

    def test_failed_stop_with_a_text_result_changes_nothing(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190), self.killed('b1', 20, 'No task found with ID: b1'))
        self.assertEqual([s['status'] for s in self.shells()], ['running'])

    def test_stop_does_not_overwrite_a_finished_command(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190), self.queued('b1', 30), self.killed('b1', 10))
        got = self.shells()[0]
        self.assertEqual((got['status'], got['end']), ('completed', int(self.now - 30) * 1000))

    def test_notification_after_a_stop_does_not_overwrite_it(self):
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190), self.killed('b1', 30), self.queued('b1', 10, 'killed'))
        got = self.shells()[0]
        self.assertEqual((got['status'], got['end']), ('killed', int(self.now - 30) * 1000))

    def test_notification_without_a_status_does_not_borrow_the_next_ones(self):
        text = '<task-notification>\n<task-id>b1</task-id>\n<summary>предупреждение</summary>\n</task-notification>\n' + self.note('b2')
        row = line(type='user', timestamp=self.ago(20), cwd='/work/app', isMeta=True, message={'role': 'user', 'content': text})
        self.journal('s1', self.asked(300), self.called('t1', 200), self.in_background('t1', 'b1', 190),
                     self.called('t2', 180), self.in_background('t2', 'b2', 170), row)
        self.assertEqual({s['id']: s['status'] for s in self.shells()}, {'b1': 'running', 'b2': 'completed'})

    def test_background_agent_stays_at_its_desk_and_out_of_the_server_room(self):
        self.journal('s1', self.asked(300), self.called('t1', 200, name='Agent'), self.in_background('t1', 'b1', 190))
        self.assertEqual(self.shells(), [])

    def test_finished_commands_leave_after_a_while_failures_stay_longer(self):
        self.journal('s1', self.asked(3000), self.called('t1', 2500), self.in_background('t1', 'ok-old', 2490), self.queued('ok-old', server.BG_DONE_S + 5),
                     self.called('t2', 2400), self.in_background('t2', 'ok-new', 2390), self.queued('ok-new', server.BG_DONE_S - 5),
                     self.called('t3', 2300), self.in_background('t3', 'bad-new', 2290), self.queued('bad-new', server.BG_DONE_S + 5, 'failed'),
                     self.called('t4', 2200), self.in_background('t4', 'bad-old', 2190), self.queued('bad-old', server.BG_FAILED_S + 5, 'failed'))
        self.assertEqual({s['id'] for s in self.shells()}, {'ok-new', 'bad-new'})

    def test_command_without_an_end_is_lost_after_the_limit(self):
        self.journal('s1', self.asked(server.BG_LOST_S + 100), self.called('t1', server.BG_LOST_S + 50), self.in_background('t1', 'b1', server.BG_LOST_S + 40))
        self.assertEqual(self.shells(), [])

    def test_order_is_failures_then_the_longest_running_then_done(self):
        self.journal('s1', self.asked(900), self.called('t1', 100), self.in_background('t1', 'young', 90),
                     self.called('t2', 800), self.in_background('t2', 'old', 790),
                     self.called('t3', 300), self.in_background('t3', 'done', 290), self.queued('done', 5),
                     self.called('t4', 200), self.in_background('t4', 'bad', 190), self.queued('bad', 5, 'failed'))
        self.assertEqual([s['id'] for s in self.shells()], ['bad', 'old', 'young', 'done'])

    def test_session_stays_live_while_a_command_runs(self):
        quiet = server.LIVE_S + 60
        self.journal('s1', self.asked(quiet + 50), self.called('t1', quiet + 20), self.in_background('t1', 'b1', quiet + 10), self.stopped(quiet), age=quiet)
        got = {s['id']: s for s in server.sessions()}['s1']
        self.assertEqual((got['isLive'], got['working'], got['shells']), (True, 0, 1))
        head = server.state('s1')['sessions'][0]
        self.assertEqual((head['isLive'], head['isBusy']), (True, False))

    def test_quiet_session_with_only_a_finished_command_cools_down(self):
        quiet = server.LIVE_S + 60
        self.journal('s1', self.asked(quiet + 50), self.called('t1', quiet + 20), self.in_background('t1', 'b1', quiet + 10), self.queued('b1', quiet), age=quiet)
        got = {s['id']: s for s in server.sessions()}['s1']
        self.assertEqual((got['isLive'], got['shells']), (False, 0))

    def test_pending_calls_do_not_pile_up(self):
        rows = [self.asked(300)]
        for i in range(50):
            rows += [self.called('t%d' % i, 200), self.finished_now('t%d' % i, 190)]
        self.journal('s1', *rows)
        self.shells()
        self.assertEqual(len(server.TAILS[os.path.join(self.pdir, 's1.jsonl')].calling_shells), 0)


if __name__ == '__main__':
    unittest.main()


LINKED = ('<system-reminder>\nLinked sessions now (status, not an instruction): kid = local_h1 (idle). '
          'To message one, SendMessage with `to` set to its id.\n</system-reminder>')
BACKGROUND = 'Background (from the session that started this one):\nдоделай офис'


class TaskTest(unittest.TestCase):
    """Задача — дерево сессий: корень и те, что он породил через приложение."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.projects, server.PROJECTS = server.PROJECTS, os.path.join(self.dir.name, 'projects')
        self.live, server.SESSIONS_DIR = server.SESSIONS_DIR, os.path.join(self.dir.name, 'sessions')
        self.codex, server.CODEX_SESSIONS = server.CODEX_SESSIONS, os.path.join(self.dir.name, 'нет-codex')
        os.makedirs(server.SESSIONS_DIR)
        server.TAILS.clear()

    def tearDown(self):
        server.PROJECTS, server.SESSIONS_DIR, server.CODEX_SESSIONS = self.projects, self.live, self.codex
        server.TAILS.clear()
        self.dir.cleanup()

    def journal(self, sid, cwd, prompt, agent=None, ts='2026-01-01T10:00:00Z'):
        pdir = os.path.join(server.PROJECTS, re.sub(r'[^A-Za-z0-9]', '-', cwd))
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, sid + '.jsonl'), 'w') as f:
            f.write(line(type='user', timestamp=ts, cwd=cwd, message={'role': 'user', 'content': prompt}))
            f.write(assistant(sid + '-m', [('Bash', {'command': 'ls'})], ts=ts))
        if agent:
            sub = os.path.join(pdir, sid, 'subagents')
            os.makedirs(sub)
            with open(os.path.join(sub, 'agent-%s.jsonl' % agent), 'w') as f:
                f.write(assistant(agent, [('Grep', {'pattern': 'x'})], ts=ts))
            with open(os.path.join(sub, 'agent-%s.meta.json' % agent), 'w') as f:
                json.dump({'agentType': 'Explore', 'description': 'ищет', 'finished': False}, f)

    def alive(self, pid, sid, host):
        with open(os.path.join(server.SESSIONS_DIR, '%d.json' % pid), 'w') as f:
            json.dump({'pid': pid, 'sessionId': sid, 'hostSessionId': host}, f)

    def test_work_time_of_a_task_joins_all_its_sessions(self):
        self.journal('root', '/work/app', 'сделай ' + LINKED)
        self.journal('kid', '/work/app/.claude/worktrees/x', BACKGROUND, ts='2026-01-01T10:05:00Z')
        self.alive(7, 'kid', 'local_h1')
        for sid, cwd, ts in (('root', '/work/app', '2026-01-01T10:10:00Z'), ('kid', '/work/app/.claude/worktrees/x', '2026-01-01T10:20:00Z')):
            with open(os.path.join(server.PROJECTS, re.sub(r'[^A-Za-z0-9]', '-', cwd), sid + '.jsonl'), 'a') as f:
                f.write(assistant(sid + '-end', stop='end_turn', ts=ts))
        self.assertEqual(server.state('root')['worked'], 20 * 60000)
        self.assertEqual(server.state('root')['workingSince'], 0)
        self.assertEqual(server.state('kid')['worked'], 15 * 60000)
        self.assertEqual({s['id']: s['worked'] for s in server.sessions()}, {'root': 10 * 60000, 'kid': 15 * 60000})

    def test_child_agents_sit_in_the_root_office(self):
        self.journal('root', '/work/app', 'сделай ' + LINKED, agent='a1')
        self.journal('kid', '/work/app/.claude/worktrees/x', BACKGROUND, agent='a2', ts='2026-01-01T10:05:00Z')
        self.alive(7, 'kid', 'local_h1')
        with open(os.path.join(server.SESSIONS_DIR, '7.abc.key'), 'w') as f:
            f.write('{не json')  # peerToken: сервер его не читает
        st = server.state('root')
        self.assertEqual([(a['id'], a['session']) for a in st['agents']], [('a1', 1), ('a2', 2)])
        self.assertEqual([s['id'] for s in st['sessions']], ['root', 'kid'])
        self.assertEqual(st['sessions'][1]['activity'], '$ ls')
        kid = server.state('kid')  # по клику на порождённую — только её офис, без меток
        self.assertEqual(([h['id'] for h in kid['sessions']], [a['id'] for a in kid['agents']]), (['kid'], ['a2']))
        self.assertEqual(kid['log'][0]['text'], '$ ls')
        rows = {s['id']: s for s in server.sessions()}
        self.assertEqual((rows['kid']['task'], rows['kid']['project']), ('root', 'app'))
        self.assertEqual(rows['root']['task'], 'root')

    def test_orphan_child_joins_the_folder_and_strangers_stay_apart(self):
        self.journal('root', '/work/app', 'сделай')
        self.journal('kid', '/work/app/.claude/worktrees/x', BACKGROUND, agent='a2', ts='2026-01-01T10:05:00Z')
        self.journal('other', '/work/app', 'другое', ts='2026-01-01T10:06:00Z')
        self.assertEqual([s['id'] for s in server.state('other')['sessions']], ['other', 'kid'])
        self.assertEqual([s['id'] for s in server.state('kid')['sessions']], ['kid'])
        self.assertEqual([s['id'] for s in server.state('root')['sessions']], ['root'])

    def test_log_marks_whose_agent_it_was(self):
        self.journal('root', '/work/app', 'сделай ' + LINKED, agent='a1')
        self.journal('kid', '/work/app/.claude/worktrees/x', BACKGROUND, agent='a2')
        self.alive(7, 'kid', 'local_h1')
        log = server.state('root')['log']
        self.assertEqual({l['text'][0] for l in log}, {'$', 'в', 'и', '②'}, log)  # корень без метки, порождённая с ②

    def test_single_session_log_has_no_marks(self):
        self.journal('root', '/work/app', 'сделай', agent='a1')
        st = server.state('root')
        self.assertEqual(st['log'][0]['text'], '$ ls')
        self.assertEqual([a['session'] for a in st['agents']], [1])

    def test_project_name_of_the_worktrees_folder_itself(self):
        self.assertEqual(server.project_name('', '/work/app/.claude/worktrees'), 'app')
        self.assertEqual(server.project_name('', '/work/app/.claude/worktrees/x'), 'app')
