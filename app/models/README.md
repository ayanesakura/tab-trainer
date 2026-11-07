# 模型

`ppocr_v4_rec.onnx`：PaddleOCR 的 PP-OCRv4 文字识别模型（中文 + 数字，ONNX 版本取自 `rapidocr-onnxruntime` 1.4.4），Apache-2.0 许可（https://github.com/PaddlePaddle/PaddleOCR）。

`app/ocr.py` 用它在本机读六线谱上的品位数字，只解码数字 0–9，用来校对 AI 的识别结果。
数字块的切分思路参考了 PDF-to-GP5（MIT，https://github.com/F100Pilot/PDF-to-GP5）的图片识别部分。
