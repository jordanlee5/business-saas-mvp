# check_admin.py
from app.database import SessionLocal
from app.models import User

db = SessionLocal()
admin = db.query(User).filter(User.username == "admin").first()
if admin:
    print(f"管理员账号存在：用户名 {admin.username}, 角色 {admin.role}")
else:
    print("管理员账号不存在")
db.close()
# 这是测试init_admint.py是能否正确打印管理员账号得测试脚本，后期看是否需要删除？