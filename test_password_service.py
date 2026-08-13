import unittest

from app.auth import get_password_hash
from app.password_service import (
    validate_first_login_password_change,
)


class FirstLoginPasswordServiceTests(
    unittest.TestCase
):
    def setUp(self):
        self.initial_password = (
            "initial123"
        )
        self.password_hash = (
            get_password_hash(
                self.initial_password
            )
        )

    def validate(
        self,
        *,
        current_password="initial123",
        new_password="newsecure123",
        confirm_password="newsecure123",
    ):
        return (
            validate_first_login_password_change(
                current_password=current_password,
                new_password=new_password,
                confirm_password=confirm_password,
                current_password_hash=(
                    self.password_hash
                ),
            )
        )

    def test_accepts_valid_password_change(self):
        self.assertIsNone(
            self.validate()
        )

    def test_rejects_wrong_current_password(self):
        self.assertEqual(
            self.validate(
                current_password="wrong123",
            ),
            "当前密码不正确",
        )

    def test_rejects_short_new_password(self):
        self.assertEqual(
            self.validate(
                new_password="short",
                confirm_password="short",
            ),
            "新密码至少需要 8 个字符",
        )

    def test_rejects_mismatched_confirmation(self):
        self.assertEqual(
            self.validate(
                confirm_password="different123",
            ),
            "两次输入的新密码不一致",
        )

    def test_rejects_reused_initial_password(self):
        self.assertEqual(
            self.validate(
                new_password="initial123",
                confirm_password="initial123",
            ),
            "新密码不能与初始密码相同",
        )


if __name__ == "__main__":
    unittest.main()