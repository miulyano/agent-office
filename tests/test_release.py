import json
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), '..')


def text(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


class VersionTest(unittest.TestCase):
    def setUp(self):
        self.version = text('VERSION').strip()

    def test_is_semver(self):
        self.assertRegex(self.version, r'^\d+\.\d+\.\d+$')

    def test_package_files_agree(self):
        lock = json.loads(text('package-lock.json'))
        self.assertEqual(json.loads(text('package.json'))['version'], self.version)
        self.assertEqual(lock['version'], self.version)
        self.assertEqual(lock['packages']['']['version'], self.version)

    def test_readme_badge_agrees(self):
        self.assertIn('badge/version-%s-' % self.version, text('README.md'))

    def test_menubar_app_reads_version_file(self):
        line = re.search(r'CFBundleShortVersionString.*', text('scripts/build-menubar.sh')).group()
        self.assertIn('VERSION', line)


class ChangelogTest(unittest.TestCase):
    def setUp(self):
        self.version = text('VERSION').strip()
        self.changelog = text('CHANGELOG.md')
        self.headings = re.findall(r'^## \[(.+?)\]', self.changelog, re.M)

    def test_unreleased_comes_first(self):
        self.assertEqual(self.headings[0], 'Unreleased')

    def test_current_version_is_the_latest_section(self):
        self.assertEqual(self.headings[1], self.version)

    def test_release_section_has_a_date_and_entries(self):
        # release.yml берёт текст релиза из этого раздела
        head = r'^## \[%s\] – \d{4}-\d{2}-\d{2}$' % re.escape(self.version)
        self.assertRegex(self.changelog, re.compile(head, re.M))
        body = re.split(head, self.changelog, flags=re.M)[1].split('\n## [')[0]
        self.assertRegex(body, re.compile(r'^- ', re.M))

    def test_every_section_links_to_its_release(self):
        for version in self.headings[1:]:
            self.assertIn('[%s]: https://github.com/miulyano/agent-office/releases/tag/v%s' % (version, version), self.changelog)
