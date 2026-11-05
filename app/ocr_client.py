"""Private OCR transport. Isolated mode never falls back to application inference."""
from pathlib import Path
import httpx

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 65536
ENDPOINT = "http://ocr:8001/recognize"


class OcrUnavailable(RuntimeError):
    pass


def recognize_remote(file_path):
    with Path(file_path).open("rb") as stream:
        payload = stream.read(MAX_IMAGE_BYTES + 1)
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise OcrUnavailable("图片不能为空且不得超过 8MB，请调整后重试。")
    try:
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(40, connect=3),
                          follow_redirects=False) as client:
            response = client.post(ENDPOINT, content=payload,
                                   headers={"Content-Type": "application/octet-stream"})
        messages = {
            429: "识别服务正在处理其他图片，请稍后重新上传失败的文件。",
            413: "图片超过 8MB，请缩小后重试。",
            422: "图片无法识别，请使用清晰的 PNG 或 JPG 图片后重试。",
            504: "图片识别超时，请裁剪无关区域后重新上传。",
        }
        if response.status_code != 200:
            raise OcrUnavailable(messages.get(response.status_code,
                                 "识别服务暂不可用，请稍后重新上传失败的文件。"))
        data = response.json()
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ValueError("Invalid OCR reply")
        return text
    except OcrUnavailable:
        raise
    except (httpx.HTTPError, ValueError):
        raise OcrUnavailable("识别服务暂不可用，请稍后重新上传失败的文件。") from None
