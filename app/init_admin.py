# app/init_admin.py
from .database import SessionLocal, engine
from .models import User
import getpass
import sys
import warnings

from .auth import get_password_hash

from .schema_readiness import assert_database_schema_ready

def init_admin():
    assert_database_schema_ready(engine)
    db = SessionLocal()
    try:
        admin_user = (
            db.query(User)
            .filter(User.username == "admin")
            .first()
        )
        if not admin_user:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("为管理员设置密码（至少 12 位，输入隐藏）：")
                confirmation = getpass.getpass("再次输入密码：")
            if len(password) < 12 or not password.strip() or password != confirmation:
                raise ValueError("密码须至少 12 位，且两次输入一致。")
            admin = User(
                username="admin",
                password_hash=get_password_hash(password),
                role="admin",
                admin_level="super_admin",
                service_rate=0.0,
                must_change_password=True,
            )
            db.add(admin)
            db.commit()
            print(
                "管理员账号已创建，用户名 admin。首次登录需修改密码。"
            )
        else:
            print("管理员账号已存在")
    finally:
        db.close()

if __name__ == "__main__":
    try:
        init_admin()
    except (ValueError, EOFError, getpass.GetPassWarning, KeyboardInterrupt):
        print("管理员创建未完成。请在交互终端输入至少 12 位且一致的密码。", file=sys.stderr)
        raise SystemExit(1)
