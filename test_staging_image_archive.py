import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import tarfile

from tools import staging_image_archive as images
from tools.staging_pg_backup import inventory


class ImageArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layer = b'synthetic-layer-only'
        self.config = json.dumps({'rootfs': {'type': 'layers', 'diff_ids': [
            'sha256:' + hashlib.sha256(self.layer).hexdigest()]}}).encode()
        self.iid = 'sha256:' + hashlib.sha256(self.config).hexdigest()
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.backup = self.root / 'backup'
        self.backup.mkdir()
        (self.backup / 'data').mkdir()
        (self.backup / 'data/session.key').write_text('virtual-only')
        (self.backup / 'database.dump').write_bytes(b'virtual-dump')
        (self.backup / 'manifest.json').write_text(json.dumps(dict(format=1,
            scope='dedicated-postgres-staging', app_image_id=self.iid, files=inventory(self.backup))))
        self.write_bundle()

    def write_bundle(self, tags=None, layer=None, extra=None, config=None, count=1):
        entry = dict(Config=self.iid[7:] + '.json', RepoTags=tags,
                     Layers=['a' * 64 + '/layer.tar'])
        files = {'manifest.json': json.dumps([entry] * count).encode(),
                 entry['Config']: config if config is not None else self.config,
                 entry['Layers'][0]: layer if layer is not None else self.layer}
        with tarfile.open(self.bundle / 'image.tar', 'w') as tar:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            if extra:
                tar.addfile(extra)
        archive = self.bundle / 'image.tar'
        (self.bundle / 'manifest.json').write_text(json.dumps(dict(format=1,
            scope='staging-application-image', image_id=self.iid,
            bytes=archive.stat().st_size, sha256=images.digest(archive))))

    def rejected_without_docker(self):
        with patch.object(images, 'docker') as docker:
            with self.assertRaises((ValueError, tarfile.TarError)):
                images.load(self.backup, self.bundle)
            docker.assert_not_called()

    def test_verify_does_not_contact_docker(self):
        with patch.object(images, 'docker') as docker:
            self.assertEqual(images.verify(self.bundle, self.iid)['image_id'], self.iid)
            docker.assert_not_called()

    def test_load_verifies_then_checks_exact_identity(self):
        with patch.object(images, 'docker', side_effect=[b'loaded', self.iid.encode()]) as docker:
            images.load(self.backup, self.bundle)
            self.assertEqual(docker.call_args_list[0].args[:2], ('image', 'load'))
            self.assertEqual(docker.call_args_list[1].args[2], self.iid)

    def test_corrupt_archive_refused(self):
        with (self.bundle / 'image.tar').open('ab') as stream:
            stream.write(b'corruption')
        self.rejected_without_docker()

    def test_recomputed_envelope_cannot_hide_corrupt_layer(self):
        self.write_bundle(layer=b'changed')
        self.rejected_without_docker()

    def test_recomputed_envelope_cannot_hide_changed_configuration(self):
        self.write_bundle(config=b'{}')
        self.rejected_without_docker()

    def test_tagged_and_multiple_images_refused(self):
        for kwargs in ({'tags': ['existing:do-not-replace']}, {'count': 2}):
            with self.subTest(kwargs=kwargs):
                self.write_bundle(**kwargs)
                self.rejected_without_docker()

    def test_paths_links_and_extra_entries_refused(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hard', tarfile.LNKTYPE),
                           ('repositories', tarfile.REGTYPE), ('manifest.json', tarfile.REGTYPE)):
            with self.subTest(name=name):
                extra = tarfile.TarInfo(name)
                extra.type = kind
                extra.linkname = 'outside'
                self.write_bundle(extra=extra)
                self.rejected_without_docker()

    def test_missing_and_extra_bundle_files_refused(self):
        extra = self.bundle / 'secret.txt'
        extra.write_text('virtual')
        self.rejected_without_docker()
        extra.unlink()
        (self.bundle / 'image.tar').unlink()
        self.rejected_without_docker()

    def test_backup_corruption_refused_before_image_operation(self):
        (self.backup / 'database.dump').write_bytes(b'changed')
        self.rejected_without_docker()

    def test_other_backup_image_refused(self):
        path = self.backup / 'manifest.json'
        value = json.loads(path.read_text())
        value['app_image_id'] = 'sha256:' + 'b' * 64
        path.write_text(json.dumps(value))
        self.rejected_without_docker()

    def test_existing_destination_never_overwritten(self):
        before = (self.bundle / 'image.tar').read_bytes()
        with patch.object(images, 'docker') as docker, self.assertRaises(ValueError):
            images.create(self.backup, self.bundle)
        docker.assert_not_called()
        self.assertEqual((self.bundle / 'image.tar').read_bytes(), before)

    def test_repository_output_refused(self):
        with patch.object(images, 'docker') as docker, self.assertRaises(ValueError):
            images.create(self.backup, images.ROOT / 'private-archive')
        docker.assert_not_called()

    def test_low_disk_refused_before_save(self):
        with patch.object(images, 'docker', side_effect=[self.iid.encode(), b'100']), \
                patch.object(images.shutil, 'disk_usage', return_value=type('Space', (), {'free': 1})()), \
                self.assertRaises(ValueError):
            images.create(self.backup, self.root / 'new')
        self.assertFalse((self.root / 'new').exists())

    def test_failed_save_not_published(self):
        with patch.object(images, 'docker', side_effect=[self.iid.encode(), b'100', RuntimeError('private-detail')]), \
                self.assertRaises(RuntimeError):
            images.create(self.backup, self.root / 'new')
        self.assertFalse((self.root / 'new').exists())

    def test_export_uses_immutable_identity_and_publishes_verified_bundle(self):
        def run(*args):
            if args[:2] == ('image', 'inspect'):
                return self.iid.encode() if args[-1] == '{{.Id}}' else b'100'
            self.assertEqual(args[:3], ('image', 'save', self.iid))
            Path(args[-1]).write_bytes((self.bundle / 'image.tar').read_bytes())
            return b''
        with patch.object(images, 'docker', side_effect=run):
            result = images.create(self.backup, self.root / 'new')
        self.assertEqual(images.verify(self.root / 'new', self.iid), result)

    def test_failed_post_load_identity_not_reported_success(self):
        with patch.object(images, 'docker', side_effect=[b'loaded', b'sha256:' + b'b'*64]), self.assertRaises(ValueError):
            images.load(self.backup, self.bundle)

    def test_cli_errors_redact_docker_details(self):
        with patch.object(images, 'load', side_effect=RuntimeError('private-detail')), \
                patch('sys.stderr', new_callable=io.StringIO) as error:
            result = images.main(['load', '--backup', str(self.backup), '--bundle', str(self.bundle)])
        self.assertEqual(result, 1)
        self.assertNotIn('private-detail', error.getvalue())

    def test_invalid_image_id_refused(self):
        for value in ('latest', 'sha256:' + 'A'*64, '--help', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                images.verify(self.bundle, value)


if __name__ == '__main__':
    unittest.main()
