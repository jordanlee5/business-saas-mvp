import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import main
from app.browser_sessions import COOKIE_NAME, LIFETIME, fingerprint
from app.database import Base
from app.models import BackofficeSession, User
from app.shared_browser_sessions import DatabaseBrowserSessions, SessionCredentialsChanged, SessionStoreUnavailable
import test_browser_sessions as existing


class SharedBrowserRouteTests(existing.BrowserSessionTests):
    def setUp(self):
        super().setUp()
        self.sessions = DatabaseBrowserSessions(self.database)
        main.browser_sessions = self.sessions

    def test_server_expiry_and_capacity_bound(self):
        store = DatabaseBrowserSessions(self.database,capacity=2)
        with self.database() as db:
            password_hash = db.get(User,self.user_id).password_hash
        with patch("app.shared_browser_sessions.time.time",return_value=100):
            tokens = [store.issue(self.user_id,password_hash) for _ in range(3)]
        with patch("app.shared_browser_sessions.time.time",return_value=100+LIFETIME-1):
            self.assertEqual(sum(store.resolve(t) is not None for t in tokens),2)
        with patch("app.shared_browser_sessions.time.time",return_value=100+LIFETIME):
            self.assertTrue(all(store.resolve(t) is None for t in tokens))

    def test_unavailable_store_denies_login_access_and_logout_without_fallback(self):
        token=self.login().cookies[COOKIE_NAME]
        error=OperationalError("private statement",{},Exception("private failure"))
        with patch.object(self.sessions,"sessions",side_effect=error):
            for method,path,data in [
                ("GET","/mall-orders",None),("GET","/logout",None),
                ("POST","/login",{"username":"operator","password":self.password})]:
                response=self.client.request(method,path,data=data)
                self.assertEqual(response.status_code,503)
                self.assertIn("no-store",response.headers["cache-control"])
                self.assertNotIn("private failure",response.text)
        self.assertIsNotNone(self.sessions.resolve(token))


class SharedBrowserStoreTests(unittest.TestCase):
    def setUp(self):
        temp=self.enterContext(tempfile.TemporaryDirectory())
        self.engine=create_engine("sqlite:///"+(Path(temp)/"sessions.db").as_posix())
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory=sessionmaker(bind=self.engine,autoflush=False)
        with self.factory.begin() as db:
            db.add(User(id=1,username="virtual",password_hash="hashed-test-credential",role="admin",is_active=True))
        self.a=DatabaseBrowserSessions(self.factory)
        self.b=DatabaseBrowserSessions(self.factory)

    def test_shared_token_hash_only_and_cross_instance_revocation(self):
        token=self.a.issue(1,"hashed-test-credential")
        self.assertEqual(self.b.resolve(token).user_id,1)
        with self.factory() as db:
            row=db.query(BackofficeSession).one()
            self.assertEqual(row.token_hash,fingerprint(token).hex())
            self.assertNotEqual(row.token_hash,token)
            self.assertNotEqual(row.credential_hash,"hashed-test-credential")
        self.b.revoke(token)
        self.assertIsNone(self.a.resolve(token))

    def test_account_update_and_revocation_commit_or_rollback_together(self):
        token=self.a.issue(1,"hashed-test-credential")
        with self.factory() as db:
            db.get(User,1).is_active=False
            self.a.revoke_user(1,db=db)
            db.rollback()
        self.assertIsNotNone(self.b.resolve(token))
        with self.factory.begin() as db:
            db.get(User,1).is_active=False
            self.b.revoke_user(1,db=db)
        with self.factory.begin() as db:
            db.get(User,1).is_active=True
        self.assertIsNone(self.a.resolve(token))

    def test_stale_credentials_cannot_issue_after_account_change(self):
        for active,password in [(False,"hashed-test-credential"),(True,"new-credential")]:
            with self.factory.begin() as db:
                user=db.get(User,1);user.is_active=active;user.password_hash=password
            with self.assertRaises(SessionCredentialsChanged):
                self.a.issue(1,"hashed-test-credential")
        with self.factory() as db:self.assertEqual(db.query(BackofficeSession).count(),0)

    def test_expired_rows_are_removed_on_next_login(self):
        with patch("app.shared_browser_sessions.time.time",return_value=100):
            token=self.a.issue(1,"hashed-test-credential")
        with patch("app.shared_browser_sessions.time.time",return_value=100+LIFETIME):
            self.assertIsNone(self.b.resolve(token))
            new=self.b.issue(1,"hashed-test-credential")
            self.assertIsNotNone(self.a.resolve(new))
        with self.factory() as db:self.assertEqual(db.query(BackofficeSession).count(),1)

    def test_invalid_tokens_do_not_contact_store(self):
        with patch.object(self.a,"sessions",side_effect=AssertionError("Unexpected database call")):
            for token in (None,"","x"*64,"a"*65,123):
                self.assertIsNone(self.a.resolve(token))
                self.a.revoke(token)


class SharedBrowserMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_user_and_downgrade_discards_only_sessions(self):
        from alembic import command
        from alembic.config import Config
        directory=self.enterContext(tempfile.TemporaryDirectory())
        engine=create_engine("sqlite:///"+(Path(directory)/"migration.db").as_posix())
        self.addCleanup(engine.dispose)
        config=Config("alembic.ini")
        config.attributes["database_url"]=str(engine.url)
        command.upgrade(config,"0016_member_rights")
        with engine.begin() as connection:
            connection.execute(text("insert into users (username,password_hash,role,is_active) values ('virtual','test','admin',1)"))
        command.upgrade(config,"head")
        with engine.begin() as connection:
            connection.execute(text("insert into backoffice_sessions values (:token,1,:credential,9999999999)"),
                               {"token":"a"*64,"credential":"b"*64})
        command.downgrade(config,"0016_member_rights")
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("select username from users")).scalar_one(),"virtual")
        command.upgrade(config,"head")
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("select count(*) from backoffice_sessions")).scalar_one(),0)
