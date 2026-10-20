"""Actual Git exports: source visibility, private-file exclusion and failed-refresh preservation."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.project_reference import ReferenceError, publish


class ProjectReferenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.git('init', '--quiet')
        (self.root / 'README.md').write_text('committed source')
        (self.root / '.env.example').write_text('RESOURCE_LIMIT=2')
        tool = self.root / 'tool'
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o700)
        self.git('add', 'README.md', '.env.example', 'tool')
        self.commit()

    def tearDown(self):
        self.directory.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self):
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null',
                 'commit', '--quiet', '-m', 'Reference fixture')

    @property
    def reference(self):
        return self.root / 'runtime/reference/hermes-dev-loop'

    def test_only_committed_source_and_executable_mode_are_exported(self):
        (self.root / 'README.md').write_text('private local edit')
        (self.root / '.env').write_text('not-a-real-secret')
        (self.root / 'untracked.txt').write_text('private untracked file')
        revision = publish(self.root)
        self.assertEqual((self.reference / 'README.md').read_text(), 'committed source')
        self.assertTrue((self.reference / 'tool').stat().st_mode & 0o100)
        self.assertTrue((self.reference / '.env.example').is_file())
        for name in ('.git', '.env', 'untracked.txt', 'runtime'):
            self.assertFalse((self.reference / name).exists())
        info = json.loads((self.reference / '.devloop-reference.json').read_text())
        self.assertEqual(info['revision'], revision)

    def test_refresh_changes_pointer_without_changing_previous_snapshot(self):
        publish(self.root)
        previous = self.reference.resolve()
        (self.root / 'README.md').write_text('updated committed source')
        self.git('add', 'README.md')
        self.commit()
        publish(self.root)
        self.assertEqual((previous / 'README.md').read_text(), 'committed source')
        self.assertEqual((self.reference / 'README.md').read_text(), 'updated committed source')

    def test_tracked_private_paths_are_rejected_without_replacing_reference(self):
        publish(self.root)
        previous = self.reference.resolve()
        for name in ('.env', '.env.production', 'runtime/gateway/auth.json', '.ssh/id_rsa'):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('dummy-sensitive-fixture')
                self.git('add', name)
                self.commit()
                with self.assertRaises(ReferenceError):
                    publish(self.root)
                self.assertEqual(self.reference.resolve(), previous)
                self.assertFalse((self.reference / name).exists())
                self.git('rm', name)
                self.commit()

    def test_tracked_symlink_is_rejected_without_following_target(self):
        publish(self.root)
        previous = self.reference.resolve()
        (self.root / 'outside-source.txt').write_text('private target')
        (self.root / 'link').symlink_to(self.root / 'outside-source.txt')
        self.git('add', 'link')
        self.commit()
        with self.assertRaisesRegex(ReferenceError, 'link or special file'):
            publish(self.root)
        self.assertEqual(self.reference.resolve(), previous)
        self.assertFalse((self.reference / 'link').exists())

    def test_symlinked_reference_storage_cannot_redirect_host_writes(self):
        (self.root / 'runtime').mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'runtime/reference').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ReferenceError, 'symlink'):
            publish(self.root)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
