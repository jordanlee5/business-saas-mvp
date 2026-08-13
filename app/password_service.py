from .auth import verify_password


MIN_PASSWORD_LENGTH = 8


def validate_first_login_password_change(
    *,
    current_password: str,
    new_password: str,
    confirm_password: str,
    current_password_hash: str,
) -> str | None:
    """
    验证首次登录修改密码表单。

    返回 None 表示验证通过；
    返回字符串表示需要展示给用户的错误。
    """
    if not verify_password(
        current_password,
        current_password_hash,
    ):
        return "当前密码不正确"

    if (
        len(new_password) < MIN_PASSWORD_LENGTH
        or not new_password.strip()
    ):
        return (
            f"新密码至少需要 "
            f"{MIN_PASSWORD_LENGTH} 个字符"
        )

    if new_password != confirm_password:
        return "两次输入的新密码不一致"

    if verify_password(
        new_password,
        current_password_hash,
    ):
        return "新密码不能与初始密码相同"

    return None