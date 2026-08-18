from app.agent.duplicate_detection import compute_fingerprint, normalize_for_fingerprint


def test_normalization_is_case_and_punctuation_insensitive():
    a = normalize_for_fingerprint("Is THIS true?!")
    b = normalize_for_fingerprint("is this true")
    assert a == b


def test_normalization_collapses_whitespace():
    a = normalize_for_fingerprint("hello    world\n\nfoo")
    b = normalize_for_fingerprint("hello world foo")
    assert a == b


def test_fingerprint_is_deterministic():
    assert compute_fingerprint("Is this true?") == compute_fingerprint("is this true")


def test_fingerprint_differs_for_different_claims():
    assert compute_fingerprint("Claim A") != compute_fingerprint("Claim B")


def test_fingerprint_is_a_sha256_hex_digest():
    fp = compute_fingerprint("anything")
    assert len(fp) == 64
    int(fp, 16)  # raises ValueError if not valid hex
