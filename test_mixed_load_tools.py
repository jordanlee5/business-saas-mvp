"""Safety and measurement checks for the disposable HTTP workload."""
import unittest
from unittest.mock import patch
from tools.mixed_load_fixture import guard
from tools.mixed_load_client import summarize, voucher


class MixedLoadToolsTests(unittest.TestCase):
    def test_missing_disposable_opt_in_refuses_before_database(self):
        with patch.dict("os.environ",{},clear=True):
            with self.assertRaises(RuntimeError):
                guard()

    def test_opt_in_outside_docker_refuses(self):
        with patch.dict("os.environ",{"MIXED_LOAD_DISPOSABLE":"fresh-docker-only"}):
            with patch("tools.mixed_load_fixture.Path.exists",return_value=False):
                with self.assertRaises(RuntimeError):
                    guard()

    def test_metrics_keep_errors_and_full_response_bytes(self):
        rows=[(200,10,100,0,0.01),(503,50,20,0.01,0.06),(404,30,40,0.02,0.05)]
        result=summarize(rows,2)
        self.assertEqual(result["statuses"],{"200":1,"503":1,"404":1})
        self.assertEqual(result["p50_ms"],30)
        self.assertEqual(result["p95_ms"],50)
        self.assertEqual(result["response_bytes"],160)
        self.assertEqual(result["requests_per_second"],1.5)
        self.assertEqual(result["last_end_offset_s"],0.06)

    def test_synthetic_ocr_image_valid_and_unique(self):
        import io
        from PIL import Image
        first=voucher("A");second=voucher("B")
        self.assertNotEqual(first,second)
        with Image.open(io.BytesIO(first)) as image:
            self.assertEqual(image.size,(1000,700))
            self.assertEqual(image.format,"PNG")


if __name__=="__main__":
    unittest.main()
