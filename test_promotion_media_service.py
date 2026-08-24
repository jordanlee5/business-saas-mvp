import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.promotion_media_service import (
    MAX_PROMOTION_IMAGE_BYTES,
    delete_promotion_image,
    save_promotion_image,
    validate_promotion_image,
)


def create_test_image(
    *,
    image_format="PNG",
    size=(120, 80),
) -> bytes:
    output = BytesIO()

    image = Image.new(
        "RGB",
        size,
        color=(37, 99, 235),
    )

    image.save(
        output,
        format=image_format,
    )

    return output.getvalue()


class PromotionMediaServiceTests(
    unittest.TestCase
):
    def setUp(self):
        self.temporary_directory = (
            tempfile.TemporaryDirectory()
        )
        self.storage_root = Path(
            self.temporary_directory.name
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_accepts_valid_png(self):
        error = validate_promotion_image(
            content=create_test_image(),
            original_filename="banner.png",
        )

        self.assertIsNone(error)

    def test_rejects_unsupported_extension(
        self,
    ):
        error = validate_promotion_image(
            content=create_test_image(),
            original_filename="banner.gif",
        )

        self.assertEqual(
            error,
            (
                "宣传页图片只允许 JPG、PNG "
                "或 WebP 格式"
            ),
        )

    def test_rejects_fake_image_content(
        self,
    ):
        error = validate_promotion_image(
            content=b"not-a-real-image",
            original_filename="banner.png",
        )

        self.assertEqual(
            error,
            "上传文件不是有效的图片",
        )

    def test_rejects_extension_mismatch(
        self,
    ):
        error = validate_promotion_image(
            content=create_test_image(
                image_format="PNG"
            ),
            original_filename="banner.jpg",
        )

        self.assertEqual(
            error,
            (
                "图片扩展名与实际图片格式不一致"
            ),
        )

    def test_rejects_oversized_file(self):
        error = validate_promotion_image(
            content=(
                b"x"
                * (
                    MAX_PROMOTION_IMAGE_BYTES
                    + 1
                )
            ),
            original_filename="large.png",
        )

        self.assertEqual(
            error,
            "宣传页图片不能超过 5 MB",
        )

    def test_rejects_invalid_image_role(
        self,
    ):
        with self.assertRaisesRegex(
            ValueError,
            "宣传页图片类型无效",
        ):
            save_promotion_image(
                content=create_test_image(),
                original_filename=(
                    "banner.png"
                ),
                promotion_page_id=1,
                image_role="unknown",
                storage_root=(
                    self.storage_root
                ),
            )

    def test_saves_as_webp_and_resizes(
        self,
    ):
        image_url = save_promotion_image(
            content=create_test_image(
                size=(3000, 1200)
            ),
            original_filename="banner.png",
            promotion_page_id=8,
            image_role="hero",
            storage_root=(
                self.storage_root
            ),
        )

        self.assertTrue(
            image_url.startswith(
                (
                    "/uploads/"
                    "promotion_pages/8/"
                    "hero_"
                )
            )
        )
        self.assertTrue(
            image_url.endswith(".webp")
        )

        relative_path = image_url.removeprefix(
            "/uploads/promotion_pages/"
        )

        stored_path = (
            self.storage_root
            / relative_path
        )

        self.assertTrue(
            stored_path.is_file()
        )

        with Image.open(
            stored_path
        ) as stored_image:
            self.assertEqual(
                stored_image.format,
                "WEBP",
            )
            self.assertLessEqual(
                max(stored_image.size),
                2400,
            )

    def test_deletes_only_safe_paths(self):
        image_url = save_promotion_image(
            content=create_test_image(),
            original_filename="logo.png",
            promotion_page_id=3,
            image_role="logo",
            storage_root=(
                self.storage_root
            ),
        )

        relative_path = image_url.removeprefix(
            "/uploads/promotion_pages/"
        )

        stored_path = (
            self.storage_root
            / relative_path
        )

        self.assertTrue(
            delete_promotion_image(
                image_url,
                storage_root=(
                    self.storage_root
                ),
            )
        )
        self.assertFalse(
            stored_path.exists()
        )

        outside_file = (
            self.storage_root.parent
            / "outside.webp"
        )
        outside_file.write_bytes(
            b"outside"
        )

        self.assertFalse(
            delete_promotion_image(
                (
                    "/uploads/"
                    "promotion_pages/"
                    "../outside.webp"
                ),
                storage_root=(
                    self.storage_root
                ),
            )
        )
        self.assertTrue(
            outside_file.exists()
        )


if __name__ == "__main__":
    unittest.main()