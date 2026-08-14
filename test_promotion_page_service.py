import unittest

from app.promotion_page_service import (
    normalize_primary_color,
    normalize_promotion_slug,
    validate_promotion_page_input,
)


class PromotionPageServiceTests(
    unittest.TestCase
):
    def validate(self, **overrides):
        data = {
            "slug": "sample-insurance",
            "company_name": "示例保险公司",
            "page_title": "安心出行保障计划",
            "primary_color": "#2563EB",
            "cta_text": "立即了解",
            "cta_url": (
                "https://example.com/details"
            ),
        }
        data.update(overrides)

        return validate_promotion_page_input(
            **data
        )

    def test_accepts_valid_external_cta(self):
        self.assertIsNone(
            self.validate()
        )

    def test_accepts_valid_internal_cta(self):
        self.assertIsNone(
            self.validate(
                cta_url="/login",
            )
        )

    def test_normalizes_slug_and_color(self):
        self.assertEqual(
            normalize_promotion_slug(
                "  Sample-Insurance  "
            ),
            "sample-insurance",
        )
        self.assertEqual(
            normalize_primary_color(
                "#2563eb"
            ),
            "#2563EB",
        )

    def test_rejects_invalid_slug(self):
        self.assertEqual(
            self.validate(
                slug="保险宣传页",
            ),
            (
                "页面短链接只能使用小写字母、"
                "数字和中划线，长度为 3～80 个字符"
            ),
        )

    def test_rejects_missing_company_name(self):
        self.assertEqual(
            self.validate(
                company_name="   ",
            ),
            "保险公司名称不能为空",
        )

    def test_rejects_missing_page_title(self):
        self.assertEqual(
            self.validate(
                page_title="",
            ),
            "宣传页标题不能为空",
        )

    def test_rejects_invalid_primary_color(self):
        self.assertEqual(
            self.validate(
                primary_color="blue",
            ),
            "主题色必须使用六位十六进制颜色",
        )

    def test_rejects_incomplete_cta(self):
        self.assertEqual(
            self.validate(
                cta_url="",
            ),
            (
                "按钮文字和按钮链接需要同时填写"
            ),
        )

    def test_rejects_unsafe_cta_urls(self):
        unsafe_urls = [
            "http://example.com",
            "javascript:alert(1)",
            "//example.com",
            "/\\example.com",
        ]

        for unsafe_url in unsafe_urls:
            with self.subTest(
                unsafe_url=unsafe_url
            ):
                self.assertEqual(
                    self.validate(
                        cta_url=unsafe_url,
                    ),
                    (
                        "按钮链接只能使用系统内部地址"
                        "或 HTTPS 地址"
                    ),
                )


if __name__ == "__main__":
    unittest.main()