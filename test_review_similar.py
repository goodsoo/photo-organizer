"""review_similar.py 테스트 — 순수 묶기 로직 + 휴지통 이동 안전성.

이미지 디코드(Pillow)는 안 건드리고, 해시는 정수로 직접 주입해 순수 로직을
검증한다. 휴지통 이동은 tmp_path 로 실제 파일을 만들어 확인.
"""

from pathlib import Path

import review_similar as r
from review_similar import hamming, group_similar, move_to_trash, TRASH_DIR


# ─── 순수: 해밍거리 ───────────────────────────────────────────────────────────

def test_hamming_zero_identical():
    assert hamming(0b1011, 0b1011) == 0


def test_hamming_counts_bit_diff():
    assert hamming(0b0000, 0b1011) == 3


# ─── 순수: 유사 묶기 ──────────────────────────────────────────────────────────

def P(name):
    return Path("/x") / name


def test_group_similar_basic():
    # a,b 는 1비트 차이(유사), c 는 멀리 떨어짐
    items = [(P("a.jpg"), 0b0000), (P("b.jpg"), 0b0001),
             (P("c.jpg"), 0b1111_1111_1111)]
    groups = group_similar(items, threshold=2)
    assert len(groups) == 1
    assert {p.name for p in groups[0]} == {"a.jpg", "b.jpg"}


def test_group_singletons_excluded():
    # 아무도 안 비슷하면 그룹 없음(1장짜리는 제외)
    items = [(P("a.jpg"), 0b0), (P("b.jpg"), 0xFFFF_FFFF_FFFF_FFFF)]
    assert group_similar(items, threshold=1) == []


def test_group_transitive_chain():
    # a~b 비슷, b~c 비슷 → a,b,c 한 묶음(union-find 전이)
    items = [(P("a.jpg"), 0b0000), (P("b.jpg"), 0b0011), (P("c.jpg"), 0b0111)]
    groups = group_similar(items, threshold=2)
    assert len(groups) == 1 and len(groups[0]) == 3


def test_group_threshold_respected():
    items = [(P("a.jpg"), 0b0000), (P("b.jpg"), 0b0011)]  # 2비트 차이
    assert group_similar(items, threshold=1) == []        # 엄격하면 안 묶임
    assert len(group_similar(items, threshold=2)) == 1     # 느슨하면 묶임


def test_group_deterministic_order():
    items = [(P("z.jpg"), 0b0), (P("a.jpg"), 0b1), (P("m.jpg"), 0b1)]
    g1 = group_similar(items, 2)
    g2 = group_similar(items, 2)
    assert [list(map(str, g)) for g in g1] == [list(map(str, g)) for g in g2]


# ─── 휴지통 이동 (영구삭제 아님) ──────────────────────────────────────────────

def _mk(root, rel, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_move_to_trash_preserves_relpath(tmp_path):
    f = _mk(tmp_path, "2024-07-15_100000/a.jpg")
    moved = move_to_trash(tmp_path, [f])
    assert moved == ["2024-07-15_100000/a.jpg"]
    assert not f.exists()  # 원위치엔 없음(이동됨)
    # 휴지통에 원래 상대경로 보존
    assert (tmp_path / TRASH_DIR / "2024-07-15_100000" / "a.jpg").is_file()


def test_move_to_trash_is_not_delete(tmp_path):
    # "삭제"가 아니라 "이동" — 내용 그대로 휴지통에 살아있음
    f = _mk(tmp_path, "ev/keepme.jpg", b"precious")
    move_to_trash(tmp_path, [f])
    assert (tmp_path / TRASH_DIR / "ev" / "keepme.jpg").read_bytes() == b"precious"


def test_move_to_trash_collision_suffix(tmp_path):
    f1 = _mk(tmp_path, "ev/a.jpg", b"one")
    move_to_trash(tmp_path, [f1])
    f2 = _mk(tmp_path, "ev/a.jpg", b"two")  # 같은 상대경로 재등장
    move_to_trash(tmp_path, [f2])
    files = sorted((tmp_path / TRASH_DIR / "ev").iterdir())
    assert len(files) == 2  # 덮어쓰기 없이 둘 다 보존


def test_move_to_trash_ignores_files_outside_root(tmp_path):
    outside = tmp_path.parent / "outside.jpg"
    outside.write_bytes(b"x")
    try:
        moved = move_to_trash(tmp_path, [outside])
        assert moved == []          # root 밖은 안전하게 무시
        assert outside.is_file()    # 안 건드림
    finally:
        outside.unlink(missing_ok=True)


def test_move_to_trash_skips_already_trashed(tmp_path):
    f = _mk(tmp_path, f"{TRASH_DIR}/already.jpg")
    moved = move_to_trash(tmp_path, [f])
    assert moved == []  # 이미 휴지통 안 → 재이동 안 함
