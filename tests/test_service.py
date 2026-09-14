import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from app import main as m


class Links(unittest.TestCase):
    def test_public_and_private_topics(self):
        cases = {
            'https://t.me/Example/123': ('example', 123),
            'https://t.me/s/Example/123?single': ('example', 123),
            'https://t.me/Example/11/123': ('example', 123),
            'https://t.me/c/123456/99': (-1000000123456, 99),
            'https://t.me/c/123456/11/99?single': (-1000000123456, 99),
        }
        for raw, want in cases.items():
            self.assertEqual(m.parse_link(raw), want)

    def test_reject_wrong_hosts_invites_and_invalid_ids(self):
        for raw in ['https://t.me.evil.test/name/1', 'https://example.org/name/1',
                    'file:///etc/passwd', 'https://t.me/+invite', 'https://t.me/example',
                    'https://t.me/c/123/0', 'https://t.me/a/1', 'https://t.me/example/-2',
                    'https://t.me@example.org/name/1', 'https://t.me:9999/name/1']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                m.parse_link(raw)


async def idle():
    await asyncio.Event().wait()


class Service(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'nas'
        self.root.mkdir()
        self.token = self.base / 'token'
        self.token.write_text('x' * 43)
        self.patches = [patch.object(m, 'DATA', self.base / 'state'),
                        patch.object(m, 'ROOTS', [self.root]),
                        patch.object(m, 'TOKEN_FILE', self.token), patch.object(m, 'worker', idle),
                        patch.object(m, 'monitor', idle)]
        for p in self.patches:
            p.start()
        m.client = None
        self.life = m.lifespan(m.app)
        await self.life.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url='http://test')
        self.headers = {'Authorization': 'Bearer ' + 'x' * 43}

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.life.__aexit__(None, None, None)
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    async def get(self, route):
        return await self.http.get('/api/' + route, headers=self.headers)

    async def post(self, route, data):
        return await self.http.post('/api/' + route, json=data, headers=self.headers)

    def fake_tg(self):
        return SimpleNamespace(is_user_authorized=AsyncMock(return_value=True),
                               get_input_entity=AsyncMock(return_value='peer'),
                               get_messages=AsyncMock(), download_media=AsyncMock())

    def add_row(self, job_id='job012345', state='queued'):
        m.db.execute('INSERT INTO jobs(id,link,directory,state,created) VALUES(?,?,?,?,0)',
                     (job_id, 'https://t.me/example/123', str(self.root), state))
        m.db.commit()
        return m.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()

    async def test_auth_required_and_health_public(self):
        self.assertEqual((await self.http.get('/api/jobs')).status_code, 401)
        self.assertEqual((await self.http.post('/api/settings', json={})).status_code, 401)
        self.assertEqual((await self.http.get('/healthz')).status_code, 200)
        self.assertEqual((await self.get('jobs')).json(), [])

    async def test_host_headers_cannot_bypass_read_authentication(self):
        for host in ['test/public', 'test/?path=', 'test/#fragment', 'test@other.example']:
            for route in ['status', 'jobs', 'folders', 'watches', 'channels']:
                with self.subTest(host=host, route=route):
                    r = await self.http.get('/api/' + route, headers={'Host': host})
                    self.assertEqual(r.status_code, 401)
                    self.assertNotIn('directory', r.json())

    async def test_host_headers_cannot_bypass_write_authentication(self):
        original = m.setting()
        calls = [
            ('settings', {'directory': str(self.root / 'unauthorized')}),
            ('telegram/code', {'phone': '+12345678901'}),
            ('telegram/login', {'code': '12345'}),
            ('telegram/connect', {}),
            ('jobs', {'links': 'https://t.me/example/123'}),
            ('jobs/missing/retry', {}), ('jobs/missing/cancel', {}),
            ('watches', {'channel': '@example'}), ('watches/1/settings', {'enabled': False}),
            ('watches/1/remove', {}),
        ]
        with patch.object(m, 'get_client', AsyncMock()) as connect:
            for path, payload in calls:
                with self.subTest(path=path):
                    r = await self.http.post('/api/' + path, json=payload,
                                             headers={'Host': 'test/public'})
                    self.assertEqual(r.status_code, 401)
            connect.assert_not_awaited()
        self.assertEqual(m.setting(), original)
        self.assertFalse((self.root / 'unauthorized').exists())

    async def test_public_allowlist_and_strict_bearer_scheme(self):
        for path in ['/api', '/unknown', '/api/status/']:
            self.assertEqual((await self.http.get(path)).status_code, 401)
        self.assertEqual((await self.http.post('/healthz')).status_code, 401)
        for authorization in ['x' * 43, 'Basic ' + 'x' * 43, 'Bearer wrong']:
            r = await self.http.get('/api/status', headers={'Authorization': authorization})
            self.assertEqual(r.status_code, 401)
        self.assertEqual((await self.get('status')).status_code, 200)

    async def test_invalid_content_length_is_rejected(self):
        for value in ['not-a-number', '-1', '65537']:
            r = await self.http.post('/api/settings', content=b'{}',
                                     headers={**self.headers, 'Content-Length': value})
            self.assertEqual(r.status_code, 413 if value == '65537' else 400)

    async def test_settings_persist_and_do_not_expose_credentials(self):
        directory = self.root / '电影'
        r = await self.post('settings', {'directory': str(directory), 'api_id': 12345, 'api_hash': 'a' * 32})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(directory.is_dir())
        status = (await self.get('status')).json()
        self.assertEqual(status['directory'], str(directory))
        self.assertTrue(status['has_api_hash'])
        self.assertNotIn('api_hash', status)
        await self.life.__aexit__(None, None, None)
        self.life = m.lifespan(m.app)
        await self.life.__aenter__()
        self.assertEqual(m.setting()['api_hash'], 'a' * 32)
        self.assertEqual(m.setting()['directory'], str(directory))

    async def test_directory_traversal_and_symlink_refused(self):
        (self.root / 'escape').symlink_to(self.base)
        for path in [str(self.base), str(self.root / '..' / 'outside'), str(self.root / 'escape' / 'secret')]:
            r = await self.post('settings', {'directory': path})
            self.assertEqual(r.status_code, 400, r.text)
        self.assertFalse((self.base / 'outside').exists())

    async def test_atomic_batch_validation_and_dedup(self):
        with patch.object(m, 'get_client', AsyncMock(return_value=self.fake_tg())):
            r = await self.post('jobs', {'links': 'https://t.me/example/123\nhttps://bad.test/foo'})
            self.assertEqual(r.status_code, 400)
            self.assertEqual((await self.get('jobs')).json(), [])
            r = await self.post('jobs', {'links': 'https://t.me/example/123\nhttps://t.me/s/example/123?single'})
            self.assertEqual(r.json(), {'added': 1, 'skipped': 1})

    async def test_jobs_snapshot_directory(self):
        self.add_row()
        r = await self.post('settings', {'directory': str(self.root / 'new')})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((await self.get('jobs')).json()[0]['directory'], str(self.root))

    async def test_cancel_queue_allows_connection_changes(self):
        self.add_row()
        data = {'directory': str(self.root), 'api_id': 123, 'api_hash': 'b' * 32}
        self.assertEqual((await self.post('settings', data)).status_code, 400)
        self.assertEqual((await self.post('jobs/job012345/cancel', {})).status_code, 200)
        self.assertEqual((await self.post('settings', data)).status_code, 200)

    async def test_restart_requeues_interrupted_download(self):
        self.add_row(state='downloading')
        await self.life.__aexit__(None, None, None)
        self.life = m.lifespan(m.app)
        await self.life.__aenter__()
        self.assertEqual((await self.get('jobs')).json()[0]['state'], 'queued')

    def media(self):
        return SimpleNamespace(document=SimpleNamespace(mime_type='video/mp4'), video=True,
                               file=SimpleNamespace(size=4, name='clip.mp4', ext='.mp4'))

    def watch_row(self):
        return m.db.execute('SELECT * FROM watches LIMIT 1').fetchone()

    async def make_watch(self, **options):
        tg = self.fake_tg()
        tg.get_entity = AsyncMock(return_value=m.types.Channel(
            id=1234567890, title='Test channel', photo=m.types.ChatPhotoEmpty(), date=None))
        tg.get_messages.return_value = [SimpleNamespace(id=10)]
        with patch.object(m, 'get_client', AsyncMock(return_value=tg)):
            response = await self.post('watches', {'channel': '@example', **options})
        self.assertEqual(response.status_code, 200, response.text)
        return tg

    async def test_watch_starts_now_filters_and_persists_cursor(self):
        tg = await self.make_watch(files=False)
        self.assertEqual(self.watch_row()['last_id'], 10)
        self.assertEqual((await self.get('jobs')).json(), [])
        video = self.media()
        video.id = 11
        attachment = SimpleNamespace(id=12, document=SimpleNamespace(mime_type='application/pdf'))
        tg.get_messages.return_value = [video, attachment, SimpleNamespace(id=13, document=None)]
        await m.scan_watch(tg, self.watch_row())
        tg.get_messages.assert_awaited_with('peer', min_id=10, reverse=True, limit=100)
        jobs = (await self.get('jobs')).json()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['link'], 'https://t.me/c/1234567890/11')
        self.assertIn('Test channel', jobs[0]['source'])
        await self.life.__aexit__(None, None, None)
        self.life = m.lifespan(m.app)
        await self.life.__aenter__()
        self.assertEqual(self.watch_row()['last_id'], 13)
        await m.scan_watch(tg, self.watch_row())
        self.assertEqual(len((await self.get('jobs')).json()), 1)

    async def test_watch_pause_edit_resume_and_remove(self):
        tg = await self.make_watch()
        target = str(self.root / 'attachments')
        self.assertEqual((await self.post('watches/1/settings', {'enabled': False, 'directory': target})).status_code, 200)
        tg.get_messages.return_value = [SimpleNamespace(id=11, document=SimpleNamespace(mime_type='application/pdf'))]
        await m.scan_watch(tg, self.watch_row())
        self.assertEqual(self.watch_row()['last_id'], 10)
        await self.post('watches/1/settings', {'enabled': True})
        await m.scan_watch(tg, self.watch_row())
        self.assertEqual((await self.get('jobs')).json()[0]['directory'], target)
        await self.post('watches/1/remove', {})
        self.assertEqual((await self.get('watches')).json(), [])
        self.assertEqual(len((await self.get('jobs')).json()), 1)

    async def test_watch_queue_full_keeps_cursor(self):
        tg = await self.make_watch()
        m.db.executemany('INSERT INTO jobs(id,link,directory,state,created) VALUES(?,?,?,?,0)',
                        [(str(i), 'https://t.me/example/'+str(i+1), str(self.root), 'queued') for i in range(500)])
        m.db.commit()
        video = self.media()
        video.id = 11
        tg.get_messages.return_value = [video]
        with self.assertRaises(m.QueueFull):
            await m.scan_watch(tg, self.watch_row())
        self.assertEqual(self.watch_row()['last_id'], 10)
        m.db.execute("UPDATE jobs SET state='done' WHERE id='0'")
        m.db.commit()
        await m.scan_watch(tg, self.watch_row())
        self.assertEqual(self.watch_row()['last_id'], 11)

    async def test_watch_rejects_duplicate_invalid_types_and_paths(self):
        tg = await self.make_watch()
        with patch.object(m, 'get_client', AsyncMock(return_value=tg)):
            self.assertEqual((await self.post('watches', {'channel': '@example'})).status_code, 400)
        for data in [{'videos': False, 'files': False}, {'directory': str(self.base)}]:
            self.assertEqual((await self.post('watches/1/settings', data)).status_code, 400)
        for raw in ['https://evil.test/example', 'https://t.me/+invite', 'https://t.me/c/0', 'https://t.me:99/example']:
            with self.assertRaises(ValueError):
                m.parse_channel(raw)
        self.assertEqual(m.parse_channel('https://t.me/c/1234567890/42'), -1001234567890)
        self.assertEqual(m.parse_channel('@example'), 'example')

    async def test_download_document_attachment(self):
        job = self.add_row()
        tg = self.fake_tg()
        tg.get_messages.return_value = SimpleNamespace(document=SimpleNamespace(mime_type='application/pdf'),
            file=SimpleNamespace(size=4, name='document.pdf', ext='.pdf'))
        async def download(msg, file, progress_callback):
            file.write(b'test')
        tg.download_media.side_effect = download
        await m.download_job(job, tg)
        stored = (await self.get('jobs')).json()[0]
        self.assertEqual(stored['state'], 'done')
        self.assertTrue(stored['filename'].endswith('.pdf'))

    async def test_download_success_and_no_overwrite(self):
        job = self.add_row()
        tg = self.fake_tg()
        tg.get_messages.return_value = self.media()
        async def download(msg, file, progress_callback):
            file.write(b'test')
            await progress_callback(4, 4)
        tg.download_media.side_effect = download
        await m.download_job(job, tg)
        stored = (await self.get('jobs')).json()[0]
        self.assertEqual(stored['state'], 'done')
        self.assertEqual(Path(stored['filename']).read_bytes(), b'test')
        self.assertEqual(list(self.root.glob('*.part')), [])
        await m.download_job(job, tg)
        self.assertEqual(tg.download_media.await_count, 1)
        Path(stored['filename']).write_bytes(b'existing data')
        with self.assertRaises(ValueError):
            await m.download_job(job, tg)
        self.assertEqual(Path(stored['filename']).read_bytes(), b'existing data')

    async def test_failed_download_cleans_partial(self):
        job = self.add_row()
        tg = self.fake_tg()
        tg.get_messages.return_value = self.media()
        async def download(msg, file, progress_callback):
            file.write(b'te')
            raise ConnectionError('interrupted')
        tg.download_media.side_effect = download
        with self.assertRaises(ConnectionError):
            await m.download_job(job, tg)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_non_video_fails_and_failed_job_can_retry(self):
        job = self.add_row(state='failed')
        tg = self.fake_tg()
        tg.get_messages.return_value = None
        with self.assertRaises(ValueError):
            await m.download_job(job, tg)
        self.assertEqual((await self.post('jobs/job012345/retry', {})).status_code, 200)
        self.assertEqual((await self.get('jobs')).json()[0]['state'], 'queued')

    async def test_premium_wait_retries_same_request_and_preserves_progress(self):
        job = self.add_row(state='downloading')
        m.update_job(job['id'], progress=40)
        partial = self.root / 'partial-video'
        partial.write_bytes(b'already-downloaded')
        request, sender = object(), object()
        waiting_client = object.__new__(m.DownloadClient)
        calls = AsyncMock(side_effect=[m.errors.FloodPremiumWaitError(None, capture=3),
                                       m.errors.FloodPremiumWaitError(None, capture=2), b'next-chunk'])
        delays = []
        async def sleep(seconds):
            delays.append(seconds)
            row = m.db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(row['state'], 'downloading')
            self.assertEqual(row['progress'], 40)
            self.assertIn('普通账号下载限速', row['error'])
            self.assertEqual(partial.read_bytes(), b'already-downloaded')
        token = m.active_download.set(job['id'])
        try:
            with patch.object(m.TelegramClient, '_call', calls), patch.object(m.asyncio, 'sleep', sleep):
                result = await waiting_client._call(sender, request)
        finally:
            m.active_download.reset(token)
        self.assertEqual(result, b'next-chunk')
        self.assertEqual(delays, [4, 3])
        self.assertEqual(calls.await_count, 3)
        for call in calls.await_args_list:
            self.assertEqual(call.args, (sender, request))
        row = m.db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(row['retry_at'], 0)
        self.assertEqual(row['error'], '')

    async def test_wait_outside_download_returns_actionable_error(self):
        waiting_client = object.__new__(m.DownloadClient)
        error = m.errors.FloodPremiumWaitError(None, capture=20)
        with patch.object(m.TelegramClient, '_call', AsyncMock(side_effect=error)):
            with self.assertRaises(m.errors.FloodPremiumWaitError):
                await waiting_client._call(object(), object())
        self.assertIn('20 秒', m.error_text(error))
        self.assertNotIn('检查验证码', m.error_text(error))

    async def test_download_wait_is_cancellable(self):
        job = self.add_row(state='downloading')
        waiting_client = object.__new__(m.DownloadClient)
        token = m.active_download.set(job['id'])
        try:
            with patch.object(m.TelegramClient, '_call', AsyncMock(side_effect=m.errors.FloodWaitError(None, capture=5))), \
                 patch.object(m.asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await waiting_client._call(object(), object())
        finally:
            m.active_download.reset(token)


if __name__ == '__main__':
    unittest.main()
