from app import parse_ordr_description

CASES = [
    (
        "[7.27 ⭐] ilymeow | Groove Coverage - Poison (Nightcore & Cut Ver.) "
        "[Tylerderp's Expert] +HDDT 74.23%",
        "compact",
        7.27,
        92.5,
    ),
    (
        "Player: MasterIO02, Map: Camellia - FLYING OUT TO THE SKY "
        "(covered by Nanahira, moimoi, Nana Takahashi) "
        "[browiec & Kuki's EXTRA] by Sotarks, song length is 4:24 "
        "(6.68 ⭐) | Accuracy: 88.41%",
        "verbose",
        6.68,
        264.0,
    ),
]

for text, expected_format, expected_star, expected_length in CASES:
    parsed = parse_ordr_description(
        text,
        fallback_length_seconds=92.5,
        fallback_accuracy=0.7423,
    )
    assert parsed["descriptionFormat"] == expected_format
    assert abs(parsed["star"] - expected_star) < 1e-9
    assert abs(parsed["lengthSeconds"] - expected_length) < 1e-9
    print(expected_format, parsed["player"], parsed["map"])

print("PASS")
