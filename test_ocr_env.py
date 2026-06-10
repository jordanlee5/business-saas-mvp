import pytesseract
from PIL import Image

print("pytesseract 导入成功")
print("Pillow 导入成功")

try:
    version = pytesseract.get_tesseract_version()
    print("Tesseract 版本：", version)
except Exception as e:
    print("Tesseract 调用失败：")
    print(e)

# ---------- OCR 测试 ----------
# 确保 test_receipt.png 在项目根目录，或者修改为实际路径
import os

img_path = "uploads/voucher1.jpg"

if os.path.exists(img_path):
    img = Image.open(img_path)
    text = pytesseract.image_to_string(img, lang="chi_sim")
    print("OCR 识别结果：")
    print(text)
else:
    print(f"找不到测试图片：{img_path}")