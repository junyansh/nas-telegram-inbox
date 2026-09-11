import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('installer', Path(__file__).parents[1] / 'setup-host.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Installer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'project'
        self.root.mkdir()
        self.patches = [patch.object(installer, 'ROOT', self.root),
                        patch.object(installer, 'CONFIG', self.root / 'install.local.json'),
                        patch.object(installer, 'OVERRIDE', self.root / 'compose.override.yaml')]
        for p in self.patches:
            p.start()
        self.cfg = installer.defaults()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_multiple_directories_preserve_host_paths_and_identity(self):
        a = self.root.parent / 'Internal media'
        b = self.root.parent / 'External media'
        cfg = installer.validate(dict(self.cfg, uid=2345, gid=3456,
                                      download_dirs=[str(a), str(b)], port=9898))
        web = installer.render_override(cfg)['services']['web']
        self.assertEqual(web['user'], '2345:3456')
        self.assertEqual(web['ports'][0]['published'], '9898')
        self.assertEqual(web['environment']['DOWNLOAD_ROOTS'], str(a) + ':' + str(b))
        for volume in web['volumes']:
            self.assertEqual(volume['source'], volume['target'])
            self.assertFalse(volume['bind']['create_host_path'])

    def test_rejects_bad_ports_root_identity_and_special_paths(self):
        invalid = [dict(port=0), dict(port=65536), dict(uid=0), dict(gid=0),
                   dict(bind_address='not-an-ip'), dict(timezone='Missing/Timezone'),
                   dict(download_dirs=[]), dict(download_dirs=['relative']),
                   dict(download_dirs=['/']), dict(download_dirs=['/app/media']),
                   dict(download_dirs=['/data']), dict(download_dirs=[str(self.root)]),
                   dict(download_dirs=[str(self.root / 'secrets' / 'exposed')]),
                   dict(download_dirs=['/media/disk:extra']), dict(download_dirs=['/media/$HOME'])]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                installer.validate(dict(self.cfg, **item))

    def test_first_config_and_repeat_preserve_choices(self):
        with patch('builtins.input', return_value=''), patch('builtins.print'):
            installer.configure()
        before = installer.CONFIG.read_bytes()
        self.assertEqual(installer.CONFIG.stat().st_mode & 0o777, 0o600)
        with patch('builtins.input', side_effect=AssertionError('Must reuse configuration')), patch('builtins.print'):
            installer.configure()
        self.assertEqual(installer.CONFIG.read_bytes(), before)
        cfg = json.loads(before)
        self.assertEqual(json.loads(installer.OVERRIDE.read_text()), installer.render_override(cfg))

    def test_prepare_preserves_token_and_existing_media_permissions(self):
        external = self.root.parent / 'external'
        external.mkdir(mode=0o755)
        sentinel = external / 'existing.mp4'
        sentinel.write_bytes(b'existing content')
        cfg = installer.validate(dict(self.cfg, download_dirs=[str(self.root / 'downloads'), str(external)]))
        installer.write_private(installer.CONFIG, cfg)
        before = external.stat()
        installer.prepare()
        token = (self.root / 'secrets/access_token').read_bytes()
        installer.prepare()
        self.assertEqual((self.root / 'secrets/access_token').read_bytes(), token)
        self.assertGreaterEqual(len(token.strip()), 32)
        self.assertEqual((self.root / 'secrets/access_token').stat().st_mode & 0o777, 0o600)
        after = external.stat()
        self.assertEqual((before.st_uid, before.st_gid, before.st_mode),
                         (after.st_uid, after.st_gid, after.st_mode))
        self.assertEqual(sentinel.read_bytes(), b'existing content')

    def test_missing_external_directory_is_not_created(self):
        missing = self.root.parent / 'unmounted-drive'
        installer.write_private(installer.CONFIG, dict(self.cfg, download_dirs=[str(missing)]))
        with self.assertRaises(ValueError):
            installer.prepare()
        self.assertFalse(missing.exists())

    def test_symlink_configuration_is_refused(self):
        other = self.root / 'other'
        other.write_text('keep')
        installer.CONFIG.symlink_to(other)
        with self.assertRaises(ValueError):
            installer.write_private(installer.CONFIG, self.cfg)
        self.assertEqual(other.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
