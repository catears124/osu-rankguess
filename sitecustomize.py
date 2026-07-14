"""Load packed models and install process-wide runtime compatibility patches."""
from __future__ import annotations

import base64
import hashlib
import json
import lzma
import os
from pathlib import Path
import threading

_lock = threading.Lock()

try:
    import onnxruntime as _ort
except Exception:  # pragma: no cover
    _ort = None

if _ort is not None and not getattr(_ort, "_rankguess_parts_patch", False):
    _original_inference_session = _ort.InferenceSession

    def _materialize_parts(path_value: str | os.PathLike[str]) -> str:
        marker_path = Path(path_value)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        part_count = int(marker["parts"])
        pieces = []
        for index in range(part_count):
            part_path = marker_path.with_name(f"{marker_path.name}.{index:03d}")
            pieces.append(part_path.read_text(encoding="ascii"))
        compressed = base64.b64decode("".join(pieces), validate=True)
        compressed_hash = hashlib.sha256(compressed).hexdigest()
        if compressed_hash != marker["sha256Compressed"]:
            raise RuntimeError(f"Compressed model checksum mismatch: {marker_path.name}")
        digest = compressed_hash[:20]
        target = Path("/tmp") / f"rankguess-{marker_path.stem}-{digest}.onnx"
        if target.exists() and target.stat().st_size == int(marker["rawBytes"]):
            return str(target)
        with _lock:
            if target.exists() and target.stat().st_size == int(marker["rawBytes"]):
                return str(target)
            raw = lzma.decompress(compressed)
            if hashlib.sha256(raw).hexdigest() != marker["sha256Raw"]:
                raise RuntimeError(f"Raw model checksum mismatch: {marker_path.name}")
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(raw)
            os.replace(temporary, target)
        return str(target)

    def _packed_inference_session(path_or_bytes, *args, **kwargs):
        if isinstance(path_or_bytes, (str, os.PathLike)) and str(path_or_bytes).endswith(".onnx.parts"):
            path_or_bytes = _materialize_parts(path_or_bytes)
        return _original_inference_session(path_or_bytes, *args, **kwargs)

    _ort.InferenceSession = _packed_inference_session
    _ort._rankguess_parts_patch = True

try:
    import ordr_recovery as _ordr_recovery
    _ordr_recovery.install()
except Exception:  # pragma: no cover - never block application startup for a recovery shim.
    pass

try:
    import community_runtime as _community_runtime
    _community_runtime.install()
except Exception:  # pragma: no cover - never block application startup for optional community features.
    pass
