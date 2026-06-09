from passlib.context import CryptContext

# 注意：这里要和 init_admin.py 里的加密方式保持一致
pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """验证用户输入的明文密码和数据库里的加密密码是否一致"""
    return pwd_context.verify(plain_password, password_hash)


def get_password_hash(password: str) -> str:
    """生成加密密码"""
    return pwd_context.hash(password)