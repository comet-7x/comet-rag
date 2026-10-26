from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path


def artifact(tmp_path: Path) -> tuple[Path, Path]:
    data, native = tmp_path / "data", tmp_path / "native"
    data.mkdir()
    native.mkdir()
    samples, parsed = [], []
    for name, text, digest in (("nist145", "A", "a" * 64), ("nist144", "B", "b" * 64)):
        samples.append(
            {
                "source": {
                    "id": name,
                    "kb_id": "kb",
                    "source_id": name,
                    "revision": f"{name}-r1",
                    "active": True,
                    "format": "pdf",
                    "title": name,
                    "sections": [],
                },
                "url": "https://example.test/sample.pdf",
                "pdf_sha256": digest,
                "pdf_bytes": 1,
                "total_pages": 1,
                "pages": [{"page_number": 1, "text": text}],
            }
        )
        parsed.append(
            {
                "source_id": name,
                "source_sha256": digest,
                "elapsed_seconds": 0.1,
                "provider": {
                    "provider": "mineru",
                    "backend": "vlm-http-client",
                    "version": "3.4.5",
                    "protocol_version": 2,
                },
                "page_status": "verified",
                "page_numbers": [1],
                "normalized_sha256": sha256(text.encode()).hexdigest(),
                "normalized_characters": 1,
                "block_count": 1,
            }
        )
        (native / f"{name}.json").write_text(
            json.dumps(
                {"markdown": text, "pages": [{"page_number": 1, "markdown": text}]}
            )
        )
    (data / "pages.json").write_text(
        json.dumps(
            {
                "version": 1,
                "extractor": "pypdf 6.10.0 fixture",
                "label_author": "provisional",
                "samples": samples,
            }
        )
    )
    (data / "judgments.json").write_text(
        json.dumps(
            {
                "version": 1,
                "evidence": [{"id": "e1", "document_id": "nist145", "quote": "A"}],
                "queries": [
                    {
                        "id": "q1",
                        "kb_id": "kb",
                        "text": "question",
                        "category": "fact",
                        "relevance": {"nist145": 2},
                        "evidence": ["e1"],
                    }
                ],
            }
        )
    )
    (native / "report.json").write_text(
        json.dumps(
            {
                "scope": "native parse fixture",
                "snapshot_sha256": sha256(
                    (data / "pages.json").read_bytes()
                ).hexdigest(),
                "samples": parsed,
            }
        )
    )
    return data, native
