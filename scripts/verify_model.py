from pathlib import Path
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
model = ROOT / "model" / "model.onnx"
smoke = np.load(ROOT / "model" / "smoke_input.npz", allow_pickle=False)
session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
outputs = session.run(None, {
    "tabular_core": smoke["tabular_core"].astype(np.float32),
    "event_sequence": smoke["event_sequence"].astype(np.float32),
    "action_windows": smoke["action_windows"].astype(np.float32),
    "replay_summary": smoke["replay_summary"].astype(np.float32),
})
expected = [
    smoke["expected_skill_prediction"],
    smoke["expected_base_prediction"],
    smoke["expected_replay_correction"],
    smoke["expected_replay_gate"],
    smoke["expected_prediction_uncertainty"],
    smoke["expected_ordinal_probabilities"],
]
max_difference = max(float(np.max(np.abs(a - b))) for a, b in zip(outputs, expected))
print(f"maximum difference: {max_difference:.8g}")
assert max_difference < 1e-4
print("PASS")
