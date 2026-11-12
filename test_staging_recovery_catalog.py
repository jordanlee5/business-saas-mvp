import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import test_staging_image_archive as image_fixtures
from tools import staging_recovery_catalog as catalog
from tools.staging_pg_backup import inventory


class CatalogTests(unittest.TestCase):
    write_bundle = image_fixtures.ImageArchiveTests.write_bundle
    write_oci = image_fixtures.OciArchiveTests.write_oci
    def setUp(self):
        image_fixtures.ImageArchiveTests.setUp(self)
        self.project = "saas-pg-test-catalog"
        self.managed = self.root / "managed"
        (self.managed / "backups").mkdir(parents=True)
        (self.managed / "project.json").write_text(json.dumps({"project":self.project}))
        (self.managed / ".backup.lock").write_bytes(b"0")
        self.run = "20261112T040000Z-" + "a"*32
        self.point = self.managed / "backups" / self.run
        self.backup.rename(self.point)
        self.backup = self.point
        self.set_image(self.iid)

    def set_image(self, identity):
        path = self.point / "manifest.json"
        data = json.loads(path.read_text());data.update(source_project=self.project,app_image_id=identity,completed_at="2026-11-12T04:00:00+00:00")
        path.write_text(json.dumps(data))

    def audit(self, **kwargs):
        return catalog.audit(self.project, [self.managed], [self.bundle], **kwargs)

    def test_referenced_image_protected_and_no_docker(self):
        before = inventory(self.root)
        with patch.object(catalog.images, "docker") as docker:
            result = self.audit(candidate_image=self.iid)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["archives"][0]["retention"], "required")
        self.assertFalse(result["deletion_authorized"])
        docker.assert_not_called()
        self.assertEqual(before, inventory(self.root))

    def test_missing_new_release_archive_blocks(self):
        result = self.audit(candidate_image="sha256:"+"b"*64)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["archives"][0]["retention"], "hold_incomplete_inventory")

    def test_missing_old_image_blocks_even_when_candidate_present(self):
        self.set_image("sha256:"+"c"*64)
        result = self.audit(candidate_image=self.iid)
        self.assertIn("recovery_image_missing", [p["code"] for p in result["problems"]])

    def test_damaged_point_unknown_entry_and_wrong_owner_block(self):
        (self.point / "database.dump").write_bytes(b"changed")
        self.assertEqual(self.audit()["status"], "blocked")
        (self.managed / "backups/unknown").mkdir()
        self.assertEqual(self.audit()["status"], "blocked")
        (self.managed / "project.json").write_text('{}')
        self.assertEqual(self.audit()["status"], "blocked")

    def test_corrupt_layer_with_updated_outer_hash_blocks(self):
        self.write_bundle(layer=b"changed")
        self.assertIn("image_archive_invalid", [p["code"] for p in self.audit()["problems"]])

    def test_oci_mapping_derived_from_full_archive(self):
        config_id = self.iid
        self.write_oci()
        result = self.audit(candidate_image=config_id)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["archives"][0]["config_id"], config_id)

    def test_encrypted_point_without_private_key(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization as s
        from tools.staging_backup_crypto import encrypt
        public = self.root / "public.pem"
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        public.write_bytes(key.public_key().public_bytes(s.Encoding.PEM,s.PublicFormat.SubjectPublicKeyInfo))
        new = self.managed / "backups" / ("20261112T050000Z-"+"b"*32)
        encrypt(self.point,new,public)
        result = self.audit()
        self.assertEqual(result["status"], "ok")
        self.assertEqual({p["backup_format"] for p in result["points"]}, {1,2})

    def test_inventory_change_during_archive_check_blocks(self):
        original = catalog.images.verify
        def change(*args):
            proof = original(*args)
            self.set_image("sha256:"+"d"*64)
            return proof
        with patch.object(catalog.images,"verify",side_effect=change):
            result=self.audit()
        self.assertIn("recovery_inventory_changed_or_busy",[p["code"] for p in result["problems"]])

    def test_conflicting_copy_same_run_blocks(self):
        other = self.root / "other"
        shutil.copytree(self.managed,other)
        p=other/"backups"/self.run/"manifest.json"
        data=json.loads(p.read_text());data["completed_at"]="2026-11-12T05:00:00+00:00";p.write_text(json.dumps(data))
        result=catalog.audit(self.project,[self.managed,other],[self.bundle])
        self.assertIn("conflicting_recovery_point",[p["code"] for p in result["problems"]])

    def test_duplicate_paths_and_empty_inventory_refused(self):
        with self.assertRaises(ValueError):
            catalog.audit(self.project,[self.managed,self.managed],[self.bundle])
        self.point.rename(self.root/"held")
        self.assertEqual(self.audit()["status"],"blocked")

    def test_unreferenced_is_review_only_never_delete(self):
        old_bundle=self.root/"old-bundle";shutil.copytree(self.bundle,old_bundle)
        self.config=b'{"rootfs":{"diff_ids":[]}}'
        import hashlib,tarfile
        self.iid='sha256:'+hashlib.sha256(self.config).hexdigest()
        # Separate zero-layer virtual image archive.
        with tarfile.open(self.bundle/'image.tar','w') as tar:
            for name,raw in {'manifest.json':json.dumps([{'Config':self.iid[7:]+'.json','RepoTags':None,'Layers':[]}]).encode(),self.iid[7:]+'.json':self.config}.items():
                item=tarfile.TarInfo(name);item.size=len(raw);tar.addfile(item,io.BytesIO(raw))
        path=self.bundle/'image.tar'
        (self.bundle/'manifest.json').write_text(json.dumps(dict(format=1,scope='staging-application-image',image_id=self.iid,bytes=path.stat().st_size,sha256=catalog.digest(path))))
        result=catalog.audit(self.project,[self.managed],[old_bundle,self.bundle])
        self.assertEqual(result['archives'][1]['retention'],'unreferenced_review_only')
        self.assertTrue(path.exists())

    def test_cli_redacts_exception_details(self):
        with patch.object(catalog,'audit',side_effect=ValueError('private-detail')),patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(catalog.main(['--project',self.project,'--root',str(self.managed)]),1)
        self.assertNotIn('private-detail',output.getvalue())
