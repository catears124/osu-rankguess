import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ort = types.ModuleType("onnxruntime")
ort.InferenceSession = object
ort.SessionOptions = object
ort.GraphOptimizationLevel = types.SimpleNamespace(ORT_ENABLE_ALL=0)
sys.modules.setdefault("onnxruntime", ort)

from app import parse_ordr_metadata

CASES = [
    {
        "name": "actual render 4663424 structured verbose with mods after star",
        "description": (
            "Player: ilymeow, Map: Charli XCX - Boom Clap (kyoto Remix) "
            "(Sped Up & Cut Ver.) [you're the magic in my veins] by riot1133, "
            "song length is 0:47 (6.45 ⭐) +HDHR | Accuracy: 81.17%"
        ),
        "title": (
            "[6.45 ⭐] ilymeow | Charli XCX - Boom Clap (kyoto Remix) "
            "(Sped Up & Cut Ver.) [you're the magic in my veins] +HDHR 81.17%"
        ),
        "metadata": {
            "mapLength": 47,
            "mapTitle": "Boom Clap (kyoto Remix) (Sped Up & Cut Ver.)",
            "replayDifficulty": "you're the magic in my veins",
            "replayMods": "HDHR",
            "replayUsername": "ilymeow",
            "mapID": 2431217,
        },
        "star": 6.45,
        "length": 47.0,
        "format": "verbose",
    },
    {
        "name": "compact title only",
        "description": None,
        "title": (
            "[7.27 ⭐] ilymeow | Groove Coverage - Poison (Nightcore & Cut Ver.) "
            "[Tylerderp's Expert] +HDDT 74.23%"
        ),
        "metadata": {
            "mapLength": 93,
            "mapTitle": "Poison (Nightcore & Cut Ver.)",
            "replayDifficulty": "Tylerderp's Expert",
            "replayMods": "HDDT",
            "replayUsername": "ilymeow",
        },
        "star": 7.27,
        "length": 93.0,
        "format": "compact",
    },
    {
        "name": "documented verbose format",
        "description": (
            "Player: MasterIO02, Map: Camellia - FLYING OUT TO THE SKY "
            "(covered by Nanahira, moimoi, Nana Takahashi) "
            "[browiec & Kuki's EXTRA] by Sotarks, song length is 4:24 "
            "(6.68 ⭐) | Accuracy: 88.41%"
        ),
        "title": None,
        "metadata": {},
        "star": 6.68,
        "length": 264.0,
        "format": "verbose",
    },
]

for case in CASES:
    parsed = parse_ordr_metadata(
        description=case["description"],
        title=case["title"],
        render_metadata=case["metadata"],
        fallback_length_seconds=92.5,
        fallback_accuracy=0.7423,
        fallback_player="fallback-player",
    )
    assert abs(parsed["star"] - case["star"]) < 1e-9, (case["name"], parsed)
    assert abs(parsed["lengthSeconds"] - case["length"]) < 1e-9, (case["name"], parsed)
    assert parsed["descriptionFormat"] == case["format"], (case["name"], parsed)
    print(case["name"], "PASS", parsed["player"], parsed["map"])

print("ALL PARSER TESTS PASS")
