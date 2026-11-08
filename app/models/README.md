# 模型

`ppocr_v5_mobile_rec.onnx`：PaddleOCR 的 PP-OCRv5 mobile 文字识别模型（中文 + 英文 + 数字，字表在 ONNX 元数据 `character` 里），
Apache-2.0 许可（https://github.com/PaddlePaddle/PaddleOCR）。ONNX 版本取自 RapidOCR 在 ModelScope 上发布的转换模型
（RapidAI/RapidOCR，Apache-2.0）：
https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/master/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx
（16631306 字节，sha256 `5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5`）。

`app/ocr.py` 用它在本机读六线谱上的品位数字（只解码 0–9、括号和 x），用来校对 AI 的识别结果。
数字块的切分思路参考了 PDF-to-GP5（MIT，https://github.com/F100Pilot/PDF-to-GP5）的图片识别部分。

换模型前先跑 `uv run python scripts/ocr_bench.py --model 新模型.onnx` 对比（2026-11 比过：之前用的 PP-OCRv4
（`rapidocr-onnxruntime` 1.4.4 带的那个）读数字和 v5 差不多，括号读得差很多；PP-OCRv5 的 en 版数字略差、括号也差）。
