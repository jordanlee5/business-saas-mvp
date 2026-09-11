import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.mall.product_media_storage import (
    MAX_PRODUCT_IMAGE_BYTES,
    delete_product_image,
    save_product_image,
    validate_product_image,
)


def create_test_image(*, image_format="PNG", size=(120, 80)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=(37, 99, 235)).save(
        output,
        format=image_format,
    )
    return output.getvalue()


class ProductMediaStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_accepts_supported_image_and_rejects_spoofed_content(self):
        self.assertIsNone(
            validate_product_image(
                content=create_test_image(),
                original_filename="product.png",
            )
        )
        self.assertEqual(
            validate_product_image(
                content=b"not-an-image",
                original_filename="product.png",
            ),
            "上传文件不是有效的图片",
        )
        self.assertEqual(
            validate_product_image(
                content=create_test_image(),
                original_filename="product.gif",
            ),
            "商品图片只允许 JPG、PNG 或 WebP 格式",
        )

    def test_rejects_extension_mismatch_and_oversized_input(self):
        self.assertEqual(
            validate_product_image(
                content=create_test_image(image_format="PNG"),
                original_filename="product.jpg",
            ),
            "图片扩展名与实际图片格式不一致",
        )
        self.assertEqual(
            validate_product_image(
                content=b"x" * (MAX_PRODUCT_IMAGE_BYTES + 1),
                original_filename="large.png",
            ),
            "商品图片不能超过 5 MB",
        )

    def test_saves_in_dedicated_product_directory_as_resized_webp(self):
        image_url = save_product_image(
            content=create_test_image(size=(3000, 1200)),
            original_filename="product.png",
            product_public_id="PRD-ABC234",
            media_role="carousel",
            storage_root=self.storage_root,
        )
        self.assertRegex(
            image_url,
            r"^/uploads/mall_products/PRD-ABC234/carousel_[0-9a-f]+\.webp$",
        )
        stored_path = self.storage_root / image_url.removeprefix(
            "/uploads/mall_products/"
        )
        self.assertTrue(stored_path.is_file())
        with Image.open(stored_path) as stored_image:
            self.assertEqual(stored_image.format, "WEBP")
            self.assertLessEqual(max(stored_image.size), 2400)

    def test_rejects_unsafe_product_id_or_unknown_role(self):
        for public_id, role, message in (
            ("../outside", "MAIN", "商品公开编号无效"),
            ("PRD-ABC234", "unknown", "商品图片用途无效"),
        ):
            with self.subTest(public_id=public_id, role=role):
                with self.assertRaisesRegex(ValueError, message):
                    save_product_image(
                        content=create_test_image(),
                        original_filename="product.png",
                        product_public_id=public_id,
                        media_role=role,
                        storage_root=self.storage_root,
                    )

    def test_delete_is_limited_to_dedicated_root(self):
        image_url = save_product_image(
            content=create_test_image(),
            original_filename="main.png",
            product_public_id="PRD-ABC234",
            media_role="MAIN",
            storage_root=self.storage_root,
        )
        self.assertTrue(
            delete_product_image(image_url, storage_root=self.storage_root)
        )
        outside_file = self.storage_root.parent / "outside.webp"
        outside_file.write_bytes(b"outside")
        self.assertFalse(
            delete_product_image(
                "/uploads/mall_products/../outside.webp",
                storage_root=self.storage_root,
            )
        )
        self.assertFalse(
            delete_product_image(
                "/uploads/promotion_pages/1/logo.webp",
                storage_root=self.storage_root,
            )
        )
        self.assertTrue(outside_file.exists())


if __name__ == "__main__":
    unittest.main()
