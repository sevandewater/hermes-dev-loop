"""Host credential import with disposable RSA keys; never uses the operator's key."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.github_app import AppError, AppSettings


@unittest.skipUnless(shutil.which('openssl'), 'Host OpenSSL is needed for RSA import validation')
class GitHubAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.keys = []
        for number in range(2):
            path = Path(cls.fixtures.name) / f'fixture-{number}.pem'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(path)],
                           check=True, capture_output=True, timeout=20)
            cls.keys.append(path.read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = AppSettings(self.root / 'config/devloop')
        self.download = self.root / 'downloaded.pem'
        self.download.write_bytes(self.keys[0])
        self.download.chmod(0o664)

    def tearDown(self):
        self.directory.cleanup()

    def test_status_is_read_only_and_publication_is_disabled(self):
        self.assertFalse(self.store.status()['publishing_enabled'])
        self.assertFalse(self.store.directory.exists())

    def test_valid_key_import_uses_private_storage_and_preserves_known_ids(self):
        self.store.configure(app_id='12345', installation_id='https://github.com/settings/installations/173550314', key_file=self.download)
        self.assertEqual(self.store.key.read_bytes(), self.keys[0])
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        for path in (self.download, self.store.key, self.store.config):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        info = self.store.status()
        self.assertEqual(info['installation_id'], 173550314)
        self.assertEqual(info['app_id'], '12345')
        self.assertFalse(info['publishing_enabled'])
        self.assertNotIn('PRIVATE KEY', json.dumps(info))

    def test_fingerprint_is_rejected_without_creating_configuration(self):
        self.download.write_text('SHA256:dummyPublicFingerprint\n')
        with self.assertRaisesRegex(AppError, 'public fingerprint'):
            self.store.configure(app_id='12345', installation_id='173550314', key_file=self.download)
        self.assertFalse(self.store.key.exists())
        self.assertFalse(self.store.config.exists())

    def test_malformed_pem_preserves_existing_configuration_and_key(self):
        self.store.configure(app_id='12345', installation_id='173550314', key_file=self.download)
        before = self.store.config.read_bytes()
        self.download.write_text('-----BEGIN ' + 'PRIVATE KEY-----\ninvalid-data\n')
        with self.assertRaises(AppError):
            self.store.configure(installation_id='99999', key_file=self.download)
        self.assertEqual(self.store.key.read_bytes(), self.keys[0])
        self.assertEqual(self.store.config.read_bytes(), before)

    def test_reimport_is_idempotent_but_rotation_does_not_overwrite_a_key(self):
        self.store.import_key(self.download)
        self.store.import_key(self.download)
        self.download.write_bytes(self.keys[1])
        with self.assertRaisesRegex(AppError, 'different key already exists'):
            self.store.import_key(self.download)
        self.assertEqual(self.store.key.read_bytes(), self.keys[0])

    def test_symlinks_cannot_redirect_key_reads_or_writes(self):
        linked_download = self.root / 'linked.pem'
        linked_download.symlink_to(self.download)
        with self.assertRaises(AppError):
            self.store.import_key(linked_download)
        self.store.secure_directory()
        self.store.key.symlink_to(self.download)
        with self.assertRaises(AppError):
            self.store.import_key(self.download)
        self.assertEqual(self.download.read_bytes(), self.keys[0])

    def test_unknown_metadata_survives_but_invalid_ids_do_not_replace_it(self):
        self.store.configure(app_id='Iv23example', installation_id='173550314')
        data = self.store.load()
        data['custom'] = 'preserve'
        self.store.config.write_text(json.dumps(data))
        before = self.store.config.read_bytes()
        with self.assertRaises(AppError):
            self.store.configure(installation_id='https://attacker.invalid/installations/2', key_file=self.download)
        self.assertEqual(self.store.config.read_bytes(), before)
        self.assertFalse(self.store.key.exists())
        self.store.configure(installation_id='173550315')
        self.assertEqual(self.store.load()['custom'], 'preserve')

    def test_insecure_directory_is_reported(self):
        self.store.configure(app_id='12345', installation_id='173550314', key_file=self.download)
        self.store.directory.chmod(0o755)
        with self.assertRaisesRegex(AppError, '0700'):
            self.store.status()


if __name__ == '__main__':
    unittest.main()
