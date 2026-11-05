"""Loopback-only virtual-member workload. Run in a fresh process from the repository root."""
import argparse
import asyncio
from collections import Counter
from datetime import timedelta
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import platform
import secrets
import sys
import tempfile
from threading import Lock
import time
from unittest.mock import patch

import httpx
from alembic import command
from alembic.config import Config

from tools.rehearse_member_activation import TestIdentities
from tools.rehearse_member_fulfillment import ADDRESS, PREFIX, serve

APP_ID = "wx0123456789abcdef"
PRICE = Decimal("8.00")


class DatabaseTiming:
    def __init__(self, engine):
        from sqlalchemy import event
        self.lock = Lock()
        self.statements = 0
        self.seconds = 0.0
        self.write_lock_seconds = 0.0
        self.errors = Counter()
        event.listen(engine, "before_cursor_execute", self.before)
        event.listen(engine, "after_cursor_execute", self.after)
        event.listen(engine, "handle_error", self.error)

    def error(self, context):
        original = context.original_exception
        code = getattr(original, "sqlite_errorname", type(original).__name__)
        with self.lock:
            self.errors[code] += 1

    def before(self, connection, cursor, statement, parameters, context, executemany):
        context.measurement_start = time.perf_counter()

    def after(self, connection, cursor, statement, parameters, context, executemany):
        elapsed = time.perf_counter() - context.measurement_start
        with self.lock:
            self.statements += 1
            self.seconds += elapsed
            if statement.startswith("BEGIN IMMEDIATE"):
                self.write_lock_seconds += elapsed

    def snapshot(self):
        with self.lock:
            return (self.statements, self.seconds, self.write_lock_seconds)


def seed(profile, count, stock):
    from app.auth import get_password_hash
    from app.database import SessionLocal
    from app.mall.activation_service import issue_one_time_activation_code
    from app.mall.inventory_service import receive_inventory
    from app.models import BusinessRecord, Product, ProductCategory, ProductSku, Supplier, UploadBatch, User
    from app.time_utils import utc8_now

    with SessionLocal.begin() as db:
        actor = User(username="load-operator", role="admin", admin_level="operator", is_active=True,
                     password_hash=get_password_hash(secrets.token_urlsafe(24)))
        partner = User(username="load-partner", role="partner", is_active=True)
        db.add_all([actor, partner])
        db.flush()
        batch = UploadBatch(user_id=partner.id, filename="synthetic-load.xlsx", acceptance_status="已承接",
                            redemption_mode="MALL_REDEMPTION", claim_deadline=utc8_now()+timedelta(days=30))
        category = ProductCategory(name="并发演练", slug="load")
        supplier = Supplier(supplier_public_id="SUP-LOAD", name="虚拟供应商")
        db.add_all([batch, category, supplier])
        db.flush()
        for product_index in range(100):
            product = Product(product_public_id=f"PRD-LOAD-{product_index}", category_id=category.id,
                              name=f"虚拟商品 {product_index}", status="PUBLISHED", published_at=utc8_now())
            db.add(product)
            db.flush()
            for sku_index in range(10):
                sku = ProductSku(product_id=product.id, supplier_id=supplier.id,
                                 sku_code=f"LOAD-{product_index}-{sku_index}", name=f"规格 {sku_index}",
                                 points_price=PRICE, cost_price=Decimal("2.00"))
                db.add(sku)
                if product_index == sku_index == 0:
                    selected = sku
        db.flush()
        receive_inventory(db, actor_admin_id=actor.id, sku_id=selected.id, quantity=stock,
                          reason="虚拟并发库存", idempotency_key="load-stock")
        credentials = []
        for index in range(count):
            business = BusinessRecord(user_id=partner.id, batch_id=batch.id, name="虚拟会员",
                public_business_no=f"BR-LOAD-{index}", business_no=f"LOAD-{index}", phone="13800000001",
                plate_number="京A00001", points_amount=Decimal("100.00"),
                redemption_mode="MALL_REDEMPTION", claim_status="PENDING_ACTIVATION")
            db.add(business)
            db.flush()
            issued = issue_one_time_activation_code(db, business_record=business)
            credentials.append((business.public_business_no, issued.activation_code))
        profile.write_text(json.dumps({"operator_name":"虚拟运营公司","operator_contact":"test@example.invalid",
            "suppliers":{"SUP-LOAD":{"enabled":True,"agreement_ref":"synthetic-only","name":"虚拟供应商"}}}), encoding="utf8")
        return credentials, selected.id


async def wave(client, label, requests, concurrency, timing):
    semaphore = asyncio.Semaphore(concurrency)
    before = timing.snapshot()
    cpu = time.process_time()
    started = time.perf_counter()

    async def perform(spec):
        async with semaphore:
            begin = time.perf_counter()
            try:
                response = await client.request(**spec)
                return response.status_code, (time.perf_counter()-begin)*1000, response
            except httpx.HTTPError:
                return 0, (time.perf_counter()-begin)*1000, None

    values = await asyncio.gather(*(perform(spec) for spec in requests))
    wall = time.perf_counter()-started
    after = timing.snapshot()
    durations = sorted(value[1] for value in values)
    result = dict(scenario=label,concurrency=concurrency,requests=len(values),
        status_counts=dict(Counter(str(value[0]) for value in values)),
        p50_ms=round(durations[math.ceil(len(values)*.5)-1],2),
        p95_ms=round(durations[math.ceil(len(values)*.95)-1],2),
        max_ms=round(max(durations),2),elapsed_seconds=round(wall,3),
        requests_per_second=round(len(values)/wall,2),
        process_cpu_seconds=round(time.process_time()-cpu,3),
        sql_statements=after[0]-before[0],sql_total_seconds=round(after[1]-before[1],3),
        sqlite_write_lock_total_seconds=round(after[2]-before[2],3))
    print(json.dumps({**result, "database_errors_so_far": dict(timing.errors)}), file=sys.stderr, flush=True)
    return result, values


async def exercise(origin, identities, credentials, sku_id, levels, timing):
    from app.database import SessionLocal
    from app.mall.inventory_service import audit_inventory_balance
    from app.mall.points_ledger_service import audit_points_account_balance
    from app.models import InventoryBalance, Member, Order, PointsAccount, PointsGrant, PointsLedgerEntry

    history = []
    offset = 0
    limits = httpx.Limits(max_connections=max(levels), max_keepalive_connections=max(levels))
    async with httpx.AsyncClient(base_url=origin+PREFIX, trust_env=False, timeout=40, limits=limits) as client:
        async def measure(label, requests, width, *, recover=False):
            result, values = await wave(client, label, requests, width, timing)
            history.append(result)
            if recover:
                pending = [index for index,value in enumerate(values) if value[0] == 503]
                if pending:
                    result, retries = await wave(client,label+"_retry",[requests[index] for index in pending],1,timing)
                    history.append(result)
                    for index,value in zip(pending,retries):
                        values[index] = value
            return values

        def activation(index):
            number, credential = credentials[index]
            return dict(method="POST",url="/activation/redeem",json=dict(
                business_public_no=number,phone="13800000001",plate_number="京A00001",
                activation_code=credential,wechat_code=identities.fresh_code("load-"+str(index))))

        for width in levels:
            requests = [dict(method="GET",url="/products?page="+str(i%5+1)) for i in range(max(60,width*3))]
            values = await measure("catalog",requests,width)
            assert all(status == 200 and response.json()["total"] == 100 for status,_,response in values)

            count = max(8,width*2)
            indices = list(range(offset,offset+count))
            values = await measure("activation",[activation(index) for index in indices],width)
            retry_indices = [index for index,value in zip(indices,values) if value[0] == 503]
            assert all(value[0] in (200,503) for value in values)
            if retry_indices:
                retried = await measure("activation_retry",[activation(index) for index in retry_indices],1)
                assert all(value[0] in (200,400) for value in retried)
            with SessionLocal() as db:
                assert db.query(PointsGrant).count() == offset+count
                assert db.query(Member).count() == offset+count
            offset += count

        # One unused credential submitted concurrently must grant exactly once.
        index = offset
        values = await measure("same_activation",[activation(index) for _ in range(max(levels))],max(levels))
        assert sum(value[0] == 200 for value in values) == 1
        assert all(value[0] in (200,400,503) for value in values)
        with SessionLocal() as db:
            assert db.query(PointsGrant).count() == offset+1

        shoppers = min(offset,max(levels))
        tokens = []
        bodies = []
        for index in range(shoppers):
            login = await client.post("/auth/wechat-session",json={"code":identities.fresh_code("load-"+str(index))})
            assert login.status_code == 200
            headers = {"Authorization":"Bearer "+login.json()["token"]}
            tokens.append(headers)
            address = await client.post("/addresses",headers=headers,json=ADDRESS)
            assert address.status_code == 201
            lines = [dict(product_public_id="PRD-LOAD-0",sku_code="LOAD-0-0",quantity=1,expected_points_price="8.00")]
            preview = await client.post("/orders/fulfillment-preview",headers=headers,json={"lines":lines})
            assert preview.status_code == 200 and preview.json()["ready"]
            bodies.append(dict(request_id=secrets.token_hex(16),address_public_id=address.json()["address_public_id"],
                               expected_address=ADDRESS,lines=lines,fulfillment_digest=preview.json()["digest"]))
        values = await measure("scarce_stock_checkout",[dict(method="POST",url="/orders",headers=headers,json=body)
            for headers,body in zip(tokens,bodies)],shoppers,recover=True)
        assert all(value[0] in (200,201,409) for value in values)
        # Reconcile ambiguous responses using the same idempotency key.
        winners = []
        for index,(status,_,response) in enumerate(values):
            if status in (200,201):
                winners.append((index,response.json()["order_public_id"]))
        with SessionLocal() as db:
            balance = db.get(InventoryBalance,sku_id)
            assert len(winners) == balance.on_hand_quantity
            assert balance.reserved_quantity == len(winners)
            assert db.query(Order).count() == len(winners)
            assert sum(account.reserved_points for account in db.query(PointsAccount)) == PRICE*len(winners)
            assert all(account.available_points+account.reserved_points == 100 for account in db.query(PointsAccount))
            assert all(audit_points_account_balance(db,account_id=account.id).is_consistent
                       for account in db.query(PointsAccount))
            assert audit_inventory_balance(db,sku_id=sku_id).is_consistent

        # Repeated submissions must return the same order and reserve nothing extra.
        requests = [dict(method="POST",url="/orders",headers=tokens[index],json=bodies[index])
                    for index,_ in winners for _ in range(2)]
        values = await measure("duplicate_checkout",requests,shoppers,recover=True)
        assert all(value[0] == 200 for value in values)
        for value,(_,order_id) in zip(values,[winner for winner in winners for _ in range(2)]):
            assert value[2].json()["order_public_id"] == order_id
        index,order_id = winners[0]
        response = await client.get("/orders/"+order_id,headers=tokens[(index+1)%shoppers])
        assert response.status_code == 404
        requests = [dict(method="POST",url="/orders/"+order_id+"/cancel",headers=tokens[index])
                    for index,order_id in winners for _ in range(2)]
        values = await measure("duplicate_cancel",requests,shoppers,recover=True)
        assert all(value[0] == 200 for value in values)
        with SessionLocal() as db:
            assert db.get(InventoryBalance,sku_id).reserved_quantity == 0
            assert all(account.available_points == 100 and account.reserved_points == 0 for account in db.query(PointsAccount))
            assert all(audit_points_account_balance(db,account_id=account.id).is_consistent
                       for account in db.query(PointsAccount))
            assert db.query(Order).filter(Order.status!="CANCELLED").count() == 0
            assert db.query(PointsLedgerEntry).filter_by(entry_type="RESERVE").count() == len(winners)
            assert db.query(PointsLedgerEntry).filter_by(entry_type="RELEASE").count() == len(winners)
            assert audit_inventory_balance(db,sku_id=sku_id).is_consistent
    unavailable = sum(row["status_counts"].get("503",0) for row in history)
    return dict(result="PASS",availability_pass=unavailable == 0,service_unavailable_responses=unavailable,
        measurements=history,database_errors=dict(timing.errors),
        correctness=dict(grants=offset+1,orders=len(winners),
        no_overselling=True,no_duplicate_grants=True,no_duplicate_reservations=True,
        cancellation_restored_balances=True,other_member_order_status=404))


def run(levels, *, postgres=False):
    if not __debug__ or "app.database" in sys.modules:
        raise RuntimeError("Use a fresh process with assertions enabled.")
    count = sum(max(8,width*2) for width in levels)+1
    with tempfile.TemporaryDirectory(prefix="mall-concurrency-") as directory:
        root = Path(directory)
        database = root/"synthetic.db"
        profile = root/"fulfillment.json"
        url = "sqlite:///"+database.as_posix()
        if postgres:
            from sqlalchemy.engine import make_url
            url = os.environ.get("MALL_LOAD_TEST_POSTGRES_URL", "")
            target = make_url(url)
            if (os.environ.get("MALL_LOAD_TEST_ALLOW_RESET") != "1"
                    or target.drivername != "postgresql+psycopg"
                    or target.host != "database" or (target.port or 5432) != 5432
                    or target.database != "mall_concurrency_test"
                    or target.username != "load_runner"):
                raise RuntimeError("Only an explicitly disposable Docker PostgreSQL target is accepted.")
            if url == os.environ.get("DATABASE_URL"):
                raise RuntimeError("The test target must differ from the application database.")
        with patch.dict(os.environ,dict(DATABASE_URL=url,
                WECHAT_MINIPROGRAM_APP_ID=APP_ID,WECHAT_MINIPROGRAM_APP_SECRET="synthetic-local-only",
                MINIPROGRAM_SESSION_SIGNING_KEY=secrets.token_hex(32),MALL_FULFILLMENT_SETTINGS=str(profile))):
            from app.database import engine
            if postgres:
                from sqlalchemy import inspect, text
                with engine.connect() as connection:
                    assert connection.execute(text("select current_database(),current_user")).one() == (
                        "mall_concurrency_test", "load_runner")
                    assert not inspect(connection).get_table_names()
            else:
                assert Path(engine.url.database).resolve() == database.resolve() and not database.exists()
            initialized = False
            try:
                command.upgrade(Config("alembic.ini"),"head")
                initialized = True
                credentials,sku_id = seed(profile,count,min(sum(max(8,w*2) for w in levels),max(levels))//2)
                from app.main import app
                timing = DatabaseTiming(engine)
                identities = TestIdentities(APP_ID)
                with patch("app.mall.wechat_identity_service._fetch_response",identities.exchange), serve(app) as origin:
                    result = asyncio.run(exercise(origin,identities,credentials,sku_id,levels,timing))
            finally:
                if postgres and initialized:
                    from app.database import Base
                    with engine.begin() as connection:
                        for table in reversed(Base.metadata.sorted_tables):
                            connection.execute(table.delete())
                    command.downgrade(Config("alembic.ini"),"base")
                    with engine.connect() as connection:
                        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
                        assert connection.execute(text("select count(*) from alembic_version")).scalar_one() == 0
                engine.dispose()
    result.update(platform=platform.platform(),python=platform.python_version(),cpu_count=os.cpu_count(),
                  catalog_products=100,catalog_skus=1000,load_model="bounded closed loop, latency excludes client semaphore queue",
                  measurement_scope="loopback HTTP, client and server share one process; synthetic WeChat exchange")
    result["database_backend"] = "postgresql" if postgres else "sqlite"
    if postgres:
        result["disposable_postgres_target_verified"] = True
        result["disposable_postgres_schema_cleaned"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels",default="1,8,24,48")
    parser.add_argument("--postgres",action="store_true",help="only a newly created disposable Docker test database")
    args = parser.parse_args()
    levels = [int(value) for value in args.levels.split(",")]
    if not levels or any(value<1 or value>64 for value in levels) or max(levels)<2:
        parser.error("Choose concurrency levels from 1 to 64 with a maximum of at least 2.")
    print(json.dumps(run(levels,postgres=args.postgres),ensure_ascii=False,indent=2))
