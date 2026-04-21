from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, Optional


class RapidOcrAdapter:
    """Self-hosted OCR adapter intended for cloud deployment.

    Requires optional runtime deps:
    - requests
    - pillow
    - rapidocr_onnxruntime

    The adapter returns raw OCR text only; business normalization stays in app.native_ocr.
    """

    def __init__(self, *, timeout: int = 20) -> None:
        self.timeout = timeout
        try:
            import requests  # noqa: F401
            from PIL import Image  # noqa: F401
            from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        except Exception as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                'RapidOcrAdapter requires requests, pillow, rapidocr_onnxruntime'
            ) from exc
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()

    def _load_bytes(self, image_ref: str) -> bytes:
        if image_ref.startswith('http://') or image_ref.startswith('https://'):
            import requests

            response = requests.get(image_ref, timeout=self.timeout)
            response.raise_for_status()
            return response.content
        with open(image_ref, 'rb') as f:
            return f.read()

    def extract_text(self, image_ref: str) -> Dict[str, Any]:
        from PIL import Image

        image_bytes = self._load_bytes(image_ref)
        image = Image.open(BytesIO(image_bytes)).convert('RGB')
        result, _ = self._engine(image)
        lines = []
        for item in result or []:
            if len(item) >= 2 and item[1]:
                lines.append(str(item[1]))
        return {
            'raw_text': '\n'.join(lines).strip(),
            'engine': 'rapidocr_onnxruntime',
            'line_count': len(lines),
        }


__all__ = ['RapidOcrAdapter']
