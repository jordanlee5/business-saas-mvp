import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from tools import staging_offsite_backup as off
from tools.staging_backup_job import write_json, exclusive_lock, BusyError
from test_staging_backup_job import virtual_backup, PROJECT


class OffsiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = off.owned_root(self.base / 'server', PROJECT, True)
        (self.root / 'backups').mkdir()
        self.local = self.base / 'offsite'

    def seed(self, n):
        rid = '202611%02dT040000Z-' % n + ('%032x' % n)
        dest = self.root / 'backups' / rid
        virtual_backup(dest)
        write_json(self.root / 'last-success.json', dict(project=PROJECT, status='ok', run_id=rid, backup='backups/' + rid))
        return rid

    def transfer(self):
        archive = self.base / 'transfer.tar'
        with archive.open('wb') as stream:
            off.export_latest(self.root, PROJECT, stream)
        return off.receive(archive, self.local, PROJECT)

    def test_copy_ack_and_idempotent_retry(self):
        rid = self.seed(1)
        result = self.transfer()
        self.assertEqual(result['run_id'], rid)
        self.assertEqual(self.transfer()['manifest_sha256'], result['manifest_sha256'])
        off.acknowledge(self.root, PROJECT, rid, result['manifest_sha256'])
        self.assertEqual(off.offsite_check(self.root, PROJECT), 'ok')
        self.assertEqual((self.local / result['backup'] / 'database.dump').read_bytes(), b'virtual-member-order-points')

    def test_retention_keeps_unacknowledged_and_latest(self):
        unacked = self.seed(1)
        for n in range(2, 7):
            self.seed(n)
            copy = self.transfer()
            off.acknowledge(self.root, PROJECT, copy['run_id'], copy['manifest_sha256'], keep=2)
        self.assertTrue((self.root / 'backups' / unacked).exists())
        self.assertEqual(len(list((self.root / 'backups').iterdir())), 3)
        off.prune(self.local, PROJECT, 2, False)
        self.assertEqual(len(list((self.local / 'backups').iterdir())), 2)
        off.latest(self.local, PROJECT)

    def test_corrupt_transfer_does_not_replace_last_good(self):
        self.seed(1)
        self.transfer()
        original = (self.local / 'last-success.json').read_bytes()
        rid = self.seed(2)
        # Archive itself can be parseable while file hashes are wrong.
        (self.root / 'backups' / rid / 'database.dump').write_bytes(b'corrupt')
        with tarfile.open(self.base / 'bad.tar', 'w') as archive:
            archive.add(self.root / 'backups' / rid, arcname=rid)
        with self.assertRaises(ValueError):
            off.receive(self.base / 'bad.tar', self.local, PROJECT)
        self.assertEqual((self.local / 'last-success.json').read_bytes(), original)
        self.assertFalse((self.local / 'backups' / rid).exists())

    def test_path_traversal_links_duplicate_and_size_rejected(self):
        rid = self.seed(1)
        for name, kind in [('../escape', 'file'), (rid + '/data/link', 'link'),
                           (rid + '/C:escape', 'file'), (rid + '/dup', 'duplicate')]:
            with self.subTest(name=name):
                with tarfile.open(self.base / 'bad.tar', 'w') as archive:
                    item = tarfile.TarInfo(name)
                    if kind == 'link':
                        item.type = tarfile.SYMTYPE
                        item.linkname = '/tmp'
                    archive.addfile(item, io.BytesIO())
                    if kind == 'duplicate':
                        archive.addfile(item, io.BytesIO())
                with self.assertRaises(ValueError):
                    off.receive(self.base / 'bad.tar', self.local, PROJECT)
        with (self.base / 'good.tar').open('wb') as stream:
            off.export_latest(self.root, PROJECT, stream)
        with self.assertRaises(ValueError):
            off.receive(self.base / 'good.tar', self.local, PROJECT, max_bytes=1)

    def test_wrong_receipt_no_deletion(self):
        rid = self.seed(1)
        with self.assertRaises(ValueError):
            off.acknowledge(self.root, PROJECT, rid, '0' * 64)
        self.assertTrue((self.root / 'backups' / rid).exists())
        self.assertFalse((self.root / 'last-offsite.json').exists())

    def test_busy_backup_never_exported(self):
        self.seed(1)
        with exclusive_lock(self.root / '.backup.lock'):
            with self.assertRaises(BusyError):
                off.export_latest(self.root, PROJECT, io.BytesIO())

    def test_wrong_owner_refused(self):
        with self.assertRaises(ValueError):
            off.owned_root(self.root, 'saas-pg-test-other')

    def test_missing_stale_and_restored_offsite_receipt(self):
        self.assertEqual(off.offsite_check(self.root, PROJECT), 'offsite_missing_or_invalid')
        rid = self.seed(1)
        result = self.transfer()
        off.acknowledge(self.root, PROJECT, rid, result['manifest_sha256'])
        future = datetime.now(timezone.utc) + timedelta(hours=37)
        self.assertEqual(off.offsite_check(self.root, PROJECT, future), 'offsite_stale')
        self.assertEqual(off.offsite_check(self.root, PROJECT), 'ok')

    def test_transfer_failure_preserves_copies_and_logs_no_credentials(self):
        self.seed(1)
        self.transfer()
        old = (self.local / 'last-success.json').read_bytes()
        config = self.base / 'config.json'
        write_json(config, dict(project=PROJECT, local_root=str(self.local), ssh='missing-command',
                   key='private-key', known_hosts='hosts', host='test-host', remote_code='/code', remote_root='/backup'))
        with patch.object(subprocess, 'Popen', side_effect=OSError('sensitive-value')):
            report = off.pull(config)
        self.assertEqual(report['status'], 'alert')
        self.assertEqual((self.local / 'last-success.json').read_bytes(), old)
        self.assertNotIn('sensitive-value', (self.local / 'last-run.json').read_text())
        self.assertFalse((self.local / '.transfer.tar').exists())


    def mock_pull(self, on_describe=None, fail_ack=False):
        config = self.base / 'config.json'
        write_json(config, dict(project=PROJECT, local_root=str(self.local), ssh='virtual-ssh',
                   key='private-key', known_hosts='hosts', host='virtual-host', remote_code='/code', remote_root='/backup'))
        calls = []

        def command(args, **kwargs):
            import shlex
            tokens = shlex.split(args[-1])
            if 'describe' in tokens:
                calls.append('describe')
                result = off.describe_latest(self.root, PROJECT)
                if on_describe:
                    result = on_describe(result)
            elif 'ack' in tokens:
                calls.append('ack')
                if fail_ack:
                    return subprocess.CompletedProcess(args, 1, b'', b'private-error')
                result = off.acknowledge(self.root, PROJECT, tokens[tokens.index('--run-id') + 1],
                                         tokens[tokens.index('--digest') + 1])
            else:
                raise AssertionError('Unexpected command')
            return subprocess.CompletedProcess(args, 0, json.dumps(result).encode(), b'')

        def exporting(args, **kwargs):
            from unittest.mock import Mock
            calls.append('export')
            stream = io.BytesIO()
            off.export_latest(self.root, PROJECT, stream)
            stream.seek(0)
            return Mock(stdout=stream, wait=Mock(return_value=0), poll=Mock(return_value=0))

        with patch.object(off.subprocess, 'run', side_effect=command), patch.object(off.subprocess, 'Popen', side_effect=exporting):
            result = off.pull(config)
        return result, calls

    def test_pull_new_then_reuse_then_new_point(self):
        first = self.seed(1)
        result, calls = self.mock_pull()
        self.assertEqual(calls, ['describe', 'export', 'ack'])
        self.assertEqual(result['transfer_mode'], 'downloaded')
        self.assertGreater(result['archive_bytes'], 0)
        result, calls = self.mock_pull()
        self.assertEqual(calls, ['describe', 'ack'])
        self.assertEqual((result['transfer_mode'], result['archive_bytes']), ('reused_verified', 0))
        second = self.seed(2)
        result, calls = self.mock_pull()
        self.assertEqual(calls, ['describe', 'export', 'ack'])
        self.assertEqual(result['run_id'], second)
        self.assertTrue((self.local / 'backups' / first).exists())

    def test_reuse_corrupt_local_fails_without_ack_or_deletion(self):
        rid = self.seed(1)
        self.mock_pull()
        receipt = (self.root / 'last-offsite.json').read_bytes()
        last_good = (self.local / 'last-success.json').read_bytes()
        damaged = self.local / 'backups' / rid / 'database.dump'
        damaged.write_bytes(b'corrupt-local')
        result, calls = self.mock_pull()
        self.assertEqual(calls, ['describe'])
        self.assertEqual((result['status'], result['code']), ('alert', 'local_verification_failed'))
        self.assertEqual((self.root / 'last-offsite.json').read_bytes(), receipt)
        self.assertEqual((self.local / 'last-success.json').read_bytes(), last_good)
        self.assertEqual(damaged.read_bytes(), b'corrupt-local')

    def test_reuse_missing_local_file_is_not_treated_as_complete(self):
        rid = self.seed(1)
        self.mock_pull()
        (self.local / 'backups' / rid / 'data/session.key').unlink()
        result, calls = self.mock_pull()
        self.assertEqual(result['status'], 'alert')
        self.assertNotIn('ack', calls)

    def test_remote_corruption_fails_before_export_or_ack(self):
        rid = self.seed(1)
        self.mock_pull()
        (self.root / 'backups' / rid / 'database.dump').write_bytes(b'corrupt-remote')
        result, calls = self.mock_pull()
        self.assertEqual((result['status'], result['code']), ('alert', 'probe_failed'))
        self.assertEqual(calls, ['describe'])

    def test_invalid_descriptors_cannot_skip_transfer(self):
        self.seed(1)
        self.mock_pull()
        for field, value in [('project', 'wrong'), ('run_id', '../escape'), ('manifest_sha256', '0'*64), ('status', 'alert')]:
            with self.subTest(field=field):
                result, calls = self.mock_pull(lambda d: {**d, field: value})
                self.assertEqual(result['status'], 'alert')
                self.assertEqual(calls, ['describe'])

    def test_new_backup_between_probe_and_export_is_verified(self):
        self.seed(1)
        def new_point(descriptor):
            self.seed(2)
            return descriptor
        result, calls = self.mock_pull(new_point)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['run_id'], off.latest(self.root, PROJECT)[0])
        self.assertEqual(calls, ['describe', 'export', 'ack'])

    def test_reuse_ack_failure_retries_without_download(self):
        self.seed(1)
        self.mock_pull()
        original = (self.local / 'last-success.json').read_bytes()
        result, calls = self.mock_pull(fail_ack=True)
        self.assertEqual((result['status'], result['code']), ('alert', 'acknowledgement_failed'))
        self.assertEqual((self.local / 'last-success.json').read_bytes(), original)
        result, calls = self.mock_pull()
        self.assertEqual(result['status'], 'ok')
        self.assertNotIn('export', calls)

    def test_describe_obeys_backup_lock(self):
        self.seed(1)
        with exclusive_lock(self.root / '.backup.lock'):
            with self.assertRaises(BusyError):
                off.describe_latest(self.root, PROJECT)

    def test_repeated_reuse_does_not_hide_stale_source(self):
        rid = self.seed(1)
        manifest_path = self.root / 'backups' / rid / 'manifest.json'
        manifest = off.read_json(manifest_path)
        manifest['completed_at'] = (datetime.now(timezone.utc) - timedelta(hours=37)).isoformat()
        write_json(manifest_path, manifest)
        self.mock_pull()
        result, calls = self.mock_pull()
        self.assertEqual(result['status'], 'ok')
        self.assertNotIn('export', calls)
        self.assertEqual(off.offsite_check(self.root, PROJECT), 'offsite_stale')


if __name__ == '__main__':
    unittest.main()
