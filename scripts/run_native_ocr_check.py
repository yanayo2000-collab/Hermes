from __future__ import annotations

import json
from pathlib import Path

from app.native_ocr import normalize_native_ocr_fields
from app.ocr_adapter import RapidOcrAdapter


def main() -> None:
    base = Path.home() / 'Desktop' / 'OCR'
    adapter = RapidOcrAdapter()
    rows = []
    for path in sorted(base.iterdir()):
        if path.is_file() and path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}:
            extracted = adapter.extract_text(str(path))
            normalized = normalize_native_ocr_fields(extracted.get('raw_text') or '')
            rows.append({
                'file': path.name,
                'engine': extracted.get('engine'),
                'line_count': extracted.get('line_count'),
                'raw_text': extracted.get('raw_text'),
                'normalized': normalized,
            })
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
