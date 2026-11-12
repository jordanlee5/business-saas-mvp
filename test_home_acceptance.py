"""Safety boundaries for the generated local acceptance package; no live DB access."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from tools import prepare_home_acceptance as prepare


class AcceptancePreparationTests(unittest.TestCase):
    def test_existing_destination_is_preserved(self):
        with TemporaryDirectory() as folder:
            marker=Path(folder)/'keep.txt';marker.write_text('preserve')
            with self.assertRaises(ValueError):prepare.build(folder,18998)
            self.assertEqual(marker.read_text(),'preserve')

    def test_business_repository_and_ancestors_are_rejected(self):
        for target in (prepare.ROOT,prepare.ROOT/'new-acceptance-package',prepare.ROOT.parent):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):prepare.build(target,18998)

    def test_invalid_port_creates_nothing(self):
        with TemporaryDirectory() as folder:
            target=Path(folder)/'new'
            for port in (0,80,65536,True,'18998'):
                with self.assertRaises(ValueError):prepare.build(target,port)
            self.assertFalse(target.exists())

    def test_new_package_copies_code_without_live_state_and_keeps_runtime_guards(self):
        with TemporaryDirectory() as folder:
            target=Path(folder)/'new';result=prepare.build(target,18998)
            self.assertTrue(result['synthetic_only'])
            self.assertFalse((target/'application/saas_mvp.db').exists())
            self.assertFalse((target/'application/uploads').exists())
            self.assertFalse((target/'application/.env').exists())
            self.assertFalse((target/'data/acceptance.db').exists())
            self.assertFalse(list(target.rglob('project.private.config.json')))
            project=json.loads((target/'miniprogram/project.config.json').read_text(encoding='utf-8'))
            original=json.loads((target/'application/miniprogram/project.config.json').read_text(encoding='utf-8'))
            self.assertEqual(project['appid'],'touristappid');self.assertFalse(project['setting']['urlCheck'])
            self.assertTrue(original['setting']['urlCheck'])
            self.assertEqual(len(list((target/'素材包').glob('*.png'))),11)
            self.assertTrue((target/'start.ps1').read_bytes().startswith(b'\xef\xbb\xbf'))
            self.assertTrue((target/'启动验收.cmd').exists())
            for relative,digest in result['production_code'].items():
                self.assertEqual(hashlib.sha256((target/'application'/relative).read_bytes()).hexdigest(),digest)
            # The phone login factory in the actual application copy has no demo override.
            actual=(target/'application/app/api/admin_phone.py').read_text(encoding='utf-8')
            self.assertIn('def get_phone_service():',actual);self.assertNotIn('virtual-only',actual)
            self.assertIn("envVersion === 'develop' && platform === 'devtools'",(target/'miniprogram/src/config.ts').read_text(encoding='utf-8'))
            self.assertTrue((target/'miniprogram/src/demo-transport.ts').read_text(encoding='utf-8').startswith('// @ts-nocheck'))


if __name__=='__main__':unittest.main()
