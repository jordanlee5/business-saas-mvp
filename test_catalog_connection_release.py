"""HTTP regression: response serialization must not hold scarce DB connections."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.api import miniprogram_v1 as api
from app.api import member_addresses as addresses


class CatalogConnectionReleaseTests(unittest.TestCase):
    def test_concurrent_responses_release_small_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=create_engine("sqlite:///"+str(Path(directory)/"virtual.db"),
                connect_args={"check_same_thread":False},pool_size=2,max_overflow=0,pool_timeout=1)
            try:
                Base.metadata.create_all(engine)
                factory=sessionmaker(bind=engine,autoflush=False)
                app=FastAPI();app.include_router(api.miniprogram_v1_router)
                app.include_router(addresses.address_router,prefix="/api/miniprogram/v1")
                async def exercise():
                    transport=httpx.ASGITransport(app=app,raise_app_exceptions=False)
                    async with httpx.AsyncClient(transport=transport,base_url="http://virtual") as client:
                        paths=["products"]*50+["addresses"]*30
                        return await asyncio.gather(*(client.get("/api/miniprogram/v1/"+p) for p in paths))
                with patch.object(api,"SessionLocal",factory),patch.object(addresses,"engine",engine):
                    responses=asyncio.run(exercise())
                self.assertEqual([r.status_code for r in responses],[200]*50+[401]*30)
                self.assertEqual(engine.pool.checkedout(),0)
            finally:engine.dispose()

    def test_owned_session_released_before_handler_returns_on_error(self):
        # The handler's response is detached data; cleanup must precede serialization.
        with tempfile.TemporaryDirectory() as directory:
            engine=create_engine("sqlite:///"+str(Path(directory)/"virtual.db"))
            try:
                Base.metadata.create_all(engine)
                with patch.object(api,"SessionLocal",sessionmaker(bind=engine)):
                    dependency=api.public_catalog_session()
                    db=next(dependency)
                    with self.assertRaises(Exception) as exc:
                        api.get_public_product("MISSING",db=db)
                    self.assertEqual(exc.exception.status_code,404)
                    self.assertEqual(engine.pool.checkedout(),0)
                    dependency.close()
            finally:engine.dispose()
