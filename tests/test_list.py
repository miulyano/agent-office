"""Логика списка сессий в src/list.ts: её тесты на node, а npm test остаётся одной командой."""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def node_version():
    node = shutil.which('node')
    if not node:
        return None
    out = subprocess.run([node, '--version'], capture_output=True, text=True).stdout.strip().lstrip('v')
    return tuple(int(x) for x in out.split('.')[:2])


class ListTest(unittest.TestCase):
    def test_list_logic(self):
        version = node_version()
        # node reads TypeScript itself since 22.6: the tests import src/list.ts, not the built web/list.js
        if not version or version < (22, 6):
            self.skipTest('нужен node 22.6+, есть %s' % (version,))
        r = subprocess.run(['node', '--experimental-strip-types', '--no-warnings', '--test', os.path.join(ROOT, 'tests', 'list.test.mjs')],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout[-4000:] + r.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
