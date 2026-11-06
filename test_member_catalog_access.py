"""Real HTTP routes: eligibility, image bypasses, checkout and retained history."""
import copy
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.exc import OperationalError

from app import models as m
from app.api import catalog_access as access
from app.mall import qualification_service as qualifications
from app.mall.product_media_storage import save_product_image, product_thumbnail_url
from app.mall.parcel_tracking import TrackingService, tracking_service
import test_miniprogram_orders as order_cases
import test_order_service as order_fixture
from test_parcel_tracking import VirtualProvider

PREFIX = "/api/miniprogram/v1"


class MemberCatalogAccessTests(unittest.TestCase):
    def setUp(self):
        self.case = order_cases.MemberOrderTests("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.f = self.case.f
        from app import main
        self.enterContext(patch("app.api.miniprogram_v1.SessionLocal", self.f.Session))
        self.enterContext(patch.object(main, "SessionLocal", self.f.Session))
        self.enterContext(patch.object(main, "get_current_user", return_value=None))
        self.storage = self.f.path.parent / "product-images"
        self.enterContext(patch("app.mall.product_media_storage.PRODUCT_IMAGE_STORAGE_ROOT", self.storage))
        content=BytesIO(); Image.new("RGB", (1400,700), "green").save(content,"PNG")
        self.url=save_product_image(content=content.getvalue(), original_filename="virtual.png",
            product_public_id=self.f.product.product_public_id, media_role="MAIN", storage_root=self.storage)
        self.thumb=product_thumbnail_url(self.url,storage_root=self.storage)
        with self.f.Session.begin() as db:
            db.query(m.ProductMedia).filter_by(product_id=self.f.product.id).delete()
            db.add(m.ProductMedia(product_id=self.f.product.id,media_role="MAIN",image_path=self.url,
                                 is_active=True,uploaded_by_id=self.f.operator.id))
        self.client=TestClient(main.app)
        self.addCleanup(self.client.close)

    def request(self, path, *, owner=0, method="GET", headers=None, **kwargs):
        auth={} if owner is None else {"Authorization":"Bearer "+self.case.tokens[owner]}
        response=self.client.request(method,path,headers={**auth,**(headers or {})},**kwargs)
        if response.status_code not in (404,405,307):
            self.assertEqual(response.headers.get("cache-control"),"no-store, private")
        return response

    def change(self, action, **kwargs):
        with self.f.Session.begin() as db:
            row=db.query(m.MemberQualification).filter_by(member_id=self.f.member.id).one()
            qualifications.update(db,actor_id=self.f.operator.id,qualification_id=row.id,version=row.version,
                action=action,reason="虚拟访问边界验证",now=order_fixture.NOW,**kwargs)

    def balances(self):
        with self.f.Session() as db:
            return (db.query(m.Order).count(), [(x.available_points,x.reserved_points) for x in db.query(m.PointsAccount)],
                    [(x.on_hand_quantity,x.reserved_quantity) for x in db.query(m.InventoryBalance)],
                    db.query(m.PointsLedgerEntry).count())

    def test_catalog_identity_and_eligibility_cannot_be_supplied_in_query(self):
        paths=[PREFIX+"/categories", PREFIX+"/products", PREFIX+"/products?q=订单&page=1&page_size=1",
               PREFIX+"/products/PRD-ORDER-001", PREFIX+"/products/PRD-MISSING"]
        for path in paths:
            for owner, status in ((None,401),(1,403)):
                response=self.request(path,owner=owner)
                self.assertEqual(response.status_code,status)
                self.assertNotIn("ORDER-SKU-001",response.text)
                self.assertNotIn(self.url,response.text)
        self.assertEqual(self.request(PREFIX+"/products?member_id=1&qualified=true",owner=1).status_code,403)
        self.assertEqual(self.request(PREFIX+"/products",headers={"Authorization":"Bearer forged"}).status_code,401)
        for path in paths[:-1]: self.assertEqual(self.request(path).status_code,200)
        self.assertEqual(self.request(paths[-1]).status_code,404)

    def test_images_original_thumbnail_head_range_and_conditionals(self):
        for url in (self.url,self.thumb):
            for method, headers in (("GET",{}),("HEAD",{}),("GET",{"Range":"bytes=0-9"}),
                                    ("GET",{"If-None-Match":"*"})):
                for owner,status in ((None,401),(1,403)):
                    response=self.request(url,owner=owner,method=method,headers=headers)
                    self.assertEqual(response.status_code,status)
                    self.assertNotIn(b"WEBP",response.content)
            full=self.request(url)
            self.assertEqual(full.status_code,200)
            self.assertEqual(full.headers["content-type"],"image/webp")
            head=self.request(url,method="HEAD"); self.assertEqual(head.status_code,200);self.assertEqual(head.content,b"")
            partial=self.request(url,headers={"Range":"bytes=0-9"})
            self.assertEqual(partial.status_code,206);self.assertEqual(partial.content,full.content[:10])
            conditional=self.request(url,headers={"If-None-Match":full.headers["etag"]})
            self.assertEqual(conditional.status_code,200) # No reusable 304 authorization shortcut.
            copied=self.request(url+"?token="+self.case.tokens[0],owner=None)
            self.assertEqual(copied.status_code,401)

    def test_disabled_expired_and_future_qualification_denies_each_request(self):
        self.assertEqual(self.request(self.thumb).status_code,200)
        self.change("DISABLE")
        for path in (PREFIX+"/categories",PREFIX+"/products",PREFIX+"/products/PRD-ORDER-001",self.url,self.thumb):
            self.assertEqual(self.request(path).status_code,403)
        self.change("ENABLE")
        self.assertEqual(self.request(self.thumb).status_code,200)
        self.change("VALIDITY",valid_from=order_fixture.NOW-timedelta(days=1),valid_until=order_fixture.NOW)
        self.assertEqual(self.request(PREFIX+"/products").status_code,403)
        self.assertEqual(self.request(self.url).status_code,403)
        self.change("VALIDITY",valid_from=order_fixture.NOW+timedelta(seconds=1),valid_until=order_fixture.NOW+timedelta(days=1))
        self.assertEqual(self.request(self.url).status_code,403)

    def test_images_fail_for_unregistered_hidden_missing_and_traversal_paths(self):
        original=self.storage / self.url.removeprefix("/uploads/mall_products/")
        extra=original.with_name("orphan.webp");extra.write_bytes(original.read_bytes())
        for path in (self.url.rsplit("/",1)[0]+"/orphan.webp", "/uploads/mall_products/PRD-ORDER-001/%2e%2e%2fsecret.webp",
                     "/uploads/mall_products/PRD-ORDER-001/..%5csecret.webp", "/static/../uploads/mall_products/private.webp"):
            self.assertNotEqual(self.request(path).status_code,200)
        with self.f.Session.begin() as db: db.query(m.ProductMedia).filter_by(image_path=self.url).one().is_active=False
        self.assertEqual(self.request(self.url).status_code,404)
        self.assertEqual(self.request(self.thumb).status_code,404)
        with self.f.Session.begin() as db:
            db.query(m.ProductMedia).filter_by(image_path=self.url).one().is_active=True
            db.get(m.Product,self.f.product.id).status="DRAFT"
        self.assertEqual(self.request(self.url).status_code,404)
        with self.f.Session.begin() as db: db.get(m.Product,self.f.product.id).status="PUBLISHED"
        original.unlink(); self.assertEqual(self.request(self.url).status_code,404)

    def test_symlink_cannot_leave_product_storage(self):
        original=self.storage / self.url.removeprefix("/uploads/mall_products/")
        external=self.f.path.parent/"private.webp";external.write_bytes(b"private sentinel")
        original.unlink()
        try: original.symlink_to(external)
        except OSError: self.skipTest("当前环境不允许创建符号链接；Linux 验证必须执行本项")
        response=self.request(self.url);self.assertEqual(response.status_code,404)
        self.assertNotIn(b"private sentinel",response.content)

    def test_operator_image_access_is_separate_from_member_access(self):
        from app import main
        for level,active,role,expected in (("operator",True,"admin",200),("super_admin",True,"admin",200),
            ("primary_reviewer",True,"admin",401),("operator",False,"admin",401),("operator",True,"partner",401)):
            user=SimpleNamespace(is_active=active,admin_level=level,role=role,must_change_password=False)
            with patch.object(main,"get_current_user",return_value=user):
                self.assertEqual(self.request(self.url,owner=None).status_code,expected)
                self.assertEqual(self.request(PREFIX+"/products",owner=None).status_code,401)
                self.assertEqual(self.request(self.url,headers={"Authorization":"Bearer forged"}).status_code,401)

    def test_new_checkout_denied_without_mutation_but_exact_replay_survives(self):
        placed=self.case.place(); before=self.balances()
        self.change("DISABLE")
        replay=self.case.call("POST",body=self.case.body)
        self.assertEqual(replay.status_code,200);self.assertEqual(replay.json()["order_public_id"],placed["order_public_id"])
        new=copy.deepcopy(self.case.body);new["request_id"]="b"*32
        self.assertEqual(self.case.call("POST",body=new).status_code,403)
        self.assertEqual(self.case.call("POST","/fulfillment-preview",{"lines":new["lines"]}).status_code,403)
        self.assertEqual(self.case.call("GET","/request/"+self.case.body["request_id"]).status_code,200)
        self.assertEqual(self.balances(),before)
        new["request_id"]=self.case.body["request_id"];new["lines"][0]["quantity"]=1
        self.assertEqual(self.case.call("POST",body=new).status_code,409)

    def test_expiry_during_checkout_rolls_back_stock_points_and_order(self):
        before=self.balances()
        with self.f.Session() as db:
            expiry=db.query(m.MemberQualification).one().valid_until
        with patch.object(access,"utc8_now",side_effect=[order_fixture.NOW,expiry]):
            self.assertEqual(self.case.call("POST",body=self.case.body).status_code,403)
        self.assertEqual(self.balances(),before)

    def test_history_logistics_rights_remain_owned_after_revoke_and_expiry(self):
        placed=self.case.place(); oid=placed["order_public_id"]
        self.f.fulfill(oid);self.f.ship(oid)
        parcels=self.case.call("GET","/"+oid).json()["parcels"]
        provider=VirtualProvider();service=TrackingService(provider)
        self.case.client.app.dependency_overrides[tracking_service]=lambda:service
        for action,kwargs in (("DISABLE",{}),("VALIDITY",dict(valid_from=order_fixture.NOW-timedelta(days=2),valid_until=order_fixture.NOW))):
            if action=="VALIDITY": self.change("ENABLE")
            self.change(action,**kwargs)
            self.assertEqual(self.case.call("GET").status_code,200)
            self.assertEqual(self.case.call("GET","/"+oid).status_code,200)
            self.assertEqual(self.case.call("GET","/"+oid,owner=1).status_code,404)
            path="/"+oid+"/parcels/"+str(parcels[0]["parcel_id"])+"/tracking"
            self.assertEqual(self.case.call("GET",path).status_code,200)
            self.assertEqual(self.case.call("GET",path,owner=1).status_code,404)
            body=dict(request_key=("c" if action=="DISABLE" else "d")*32,kind="ACCESS",description="虚拟历史权益查询")
            created=self.request(PREFIX+"/rights-requests",method="POST",json=body)
            self.assertEqual(created.status_code,201)
            rid=created.json()["public_id"]
            self.assertEqual(self.request(PREFIX+"/rights-requests/"+rid).status_code,200)
            self.assertEqual(self.request(PREFIX+"/rights-requests/"+rid,owner=1).status_code,404)
            self.assertEqual(self.request(PREFIX+"/rights-requests").status_code,200)

    def test_database_failure_never_opens_catalog_images_or_checkout(self):
        before=self.balances()
        def fail(*args,**kwargs): raise OperationalError("private-query",{},Exception("private-database-detail"))
        with patch.object(access,"require_qualification",side_effect=fail):
            for path in (PREFIX+"/categories",PREFIX+"/products",self.url,self.thumb):
                result=self.request(path);self.assertEqual(result.status_code,503)
                self.assertNotIn("private-database",result.text)
        with patch("app.api.member_orders.require_qualification",side_effect=fail):
            self.assertEqual(self.case.call("POST",body=self.case.body).status_code,503)
        self.assertEqual(self.balances(),before)
        self.assertEqual(self.request(self.thumb).status_code,200)
        self.assertEqual(self.case.call("POST",body=self.case.body).status_code,201)

    def test_another_effective_source_survives_single_source_revocation(self):
        with self.f.Session.begin() as db:
            business=db.get(m.BusinessRecord,self.f.later_grant.business_record_id)
            qualifications.confirm(db,actor_id=self.f.operator.id,batch_id=business.batch_id,business_ids=[business.id],
                valid_from=order_fixture.NOW-timedelta(days=1),valid_until=order_fixture.NOW+timedelta(days=1),reason="第二虚拟来源",now=order_fixture.NOW)
            first=db.query(m.MemberQualification).order_by(m.MemberQualification.id).first()
            qualifications.update(db,actor_id=self.f.operator.id,qualification_id=first.id,version=first.version,action="DISABLE",reason="单来源停用",now=order_fixture.NOW)
            # Eligibility is independent from the account's available balance.
            db.get(m.PointsAccount,self.f.account.id).available_points=0
        self.assertEqual(self.request(PREFIX+"/products").status_code,200)
        self.assertEqual(self.request(self.thumb).status_code,200)


if __name__=="__main__": unittest.main()
