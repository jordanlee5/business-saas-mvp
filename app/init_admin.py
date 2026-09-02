# app/init_admin.py
from .database import SessionLocal, engine
from .models import User
from passlib.context import CryptContext

from .schema_readiness import assert_database_schema_ready

pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")

def get_password_hash(password):
    return pwd_context.hash(password)

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
            admin = User(
                username="admin",
                password_hash=get_password_hash("admin123"),
                role="admin",
                admin_level="super_admin",
                service_rate=0.0,
                must_change_password=True,
            )
            db.add(admin)
            db.commit()
            print(
                "管理员账号已创建：用户名 admin，密码 admin123"
            )
        else:
            print("管理员账号已存在")
    finally:
        db.close()

if __name__ == "__main__":
    init_admin()
