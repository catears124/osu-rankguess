from __future__ import annotations

import base64
import hashlib
import json
import lzma
import os
import threading
from pathlib import Path

_lock = threading.Lock()
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return

    try:
        import onnxruntime as ort
    except Exception:
        _installed = True
        return

    if getattr(ort, "_rankguess_parts_patch", False):
        _installed = True
        return

    original_session = ort.InferenceSession

    def materialize(path_value: str | os.PathLike[str]) -> str:
        marker_path = Path(path_value)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        pieces = [
            marker_path.with_name(f"{marker_path.name}.{index:03d}").read_text(encoding="ascii")
            for index in range(int(marker["parts"]))
        ]
        compressed = base64.b64decode("".join(pieces), validate=True)
        compressed_hash = hashlib.sha256(compressed).hexdigest()
        if compressed_hash != marker["sha256Compressed"]:
            raise RuntimeError(f"Compressed model checksum mismatch: {marker_path.name}")

        target = Path("/tmp") / f"rankguess-{marker_path.stem}-{compressed_hash[:20]}.onnx"
        expected_size = int(marker["rawBytes"])
        if target.exists() and target.stat().st_size == expected_size:
            return str(target)

        with _lock:
            if target.exists() and target.stat().st_size == expected_size:
                return str(target)
            raw = lzma.decompress(compressed)
            if hashlib.sha256(raw).hexdigest() != marker["sha256Raw"]:
                raise RuntimeError(f"Raw model checksum mismatch: {marker_path.name}")
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(raw)
            os.replace(temporary, target)
        return str(target)

    def packed_session(path_or_bytes, *args, **kwargs):
        if isinstance(path_or_bytes, (str, os.PathLike)) and str(path_or_bytes).endswith(".onnx.parts"):
            path_or_bytes = materialize(path_or_bytes)
        return original_session(path_or_bytes, *args, **kwargs)

    ort.InferenceSession = packed_session
    ort._rankguess_parts_patch = True
    _installed = True
