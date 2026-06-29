"""organize.py 테스트 — DESIGN.md 의 24경로(★★★ 3종 포함).

순수로직은 직접, IO 는 tmp_path 픽스처로. exiftool 은 메타 dict 를 직접
주입해 우회(설치 불필요). `pytest test_organize.py -v`.
"""

import datetime as dt
import hashlib
import os
import sqlite3
import unicodedata
from pathlib import Path

import pytest

import organize as o
from organize import (
    MediaFile, Unit, pick_capture_time, pair_live_photos, split_events,
    event_key, new_filename, base_name_for, sanitize_component, build_plan,
    copy_one, CopyResult, plan_from_source, is_junk,
    QUARANTINE_DIR, JUNK_DIR,
)


# ─── 헬퍼 ────────────────────────────────────────────────────────────────────

def mf(name: str, **meta) -> MediaFile:
    """메타 dict 를 주입한 MediaFile + 시각추출 수행."""
    m = MediaFile(src=Path("/src") / name, meta=meta)
    pick_capture_time(m)
    return m


def dto(s: str) -> str:
    return s  # 가독성용: exif 날짜 문자열 그대로


def write_jpg(path: Path, content: bytes = b"\xff\xd8jpegdata\xff\xd9") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ─── [T3] 시각추출 타입별 분기 / UTC격리 / 더티값 ─────────────────────────────

def test_photo_uses_datetimeoriginal():
    m = mf("a.heic", DateTimeOriginal="2024:07:15 14:30:22")
    assert m.capture_time == dt.datetime(2024, 7, 15, 14, 30, 22)
    assert m.time_source == "DateTimeOriginal"
    assert not m.is_quarantined


def test_photo_falls_back_to_createdate():
    m = mf("a.jpg", CreateDate="2024:01:02 03:04:05")
    assert m.capture_time == dt.datetime(2024, 1, 2, 3, 4, 5)
    assert m.time_source == "CreateDate"


def test_video_uses_creationdate_local():
    m = mf("v.mov", CreationDate="2024:07:15 14:30:22+09:00")
    # M1: 오프셋 있어도 로컬시각 그대로(naive)
    assert m.capture_time == dt.datetime(2024, 7, 15, 14, 30, 22)
    assert not m.is_quarantined


def test_video_utc_only_is_quarantined():
    # CreationDate 없이 UTC 계열(CreateDate)만 → 격리(N1)
    m = mf("v.mp4", CreateDate="2024:07:15 05:30:22", MediaCreateDate="2024:07:15 05:30:22")
    assert m.is_quarantined
    assert m.quarantine_reason == "video_utc_only"


def test_dirty_zero_date_is_quarantined():
    m = mf("a.heic", DateTimeOriginal="0000:00:00 00:00:00")
    assert m.is_quarantined
    assert m.quarantine_reason == "no_capture_time"


def test_partial_or_broken_date_is_quarantined():
    m = mf("a.heic", DateTimeOriginal="2024:13:40 99:99:99")  # 13월·40일
    assert m.is_quarantined


def test_no_time_tag_is_quarantined():
    m = mf("screenshot.png", ImageWidth=100)
    assert m.is_quarantined
    assert m.quarantine_reason == "no_capture_time"


# ─── [T4] 라이브포토 짝 묶기 ──────────────────────────────────────────────────

def test_live_photo_pair_grouped_by_stem():
    photo = mf("IMG_1.heic", DateTimeOriginal="2024:07:15 14:30:22")
    video = mf("IMG_1.mov", CreationDate="2024:07:15 14:30:22+09:00")
    units = pair_live_photos([photo, video])
    paired = [u for u in units if len(u.files) == 2]
    assert len(paired) == 1
    assert paired[0].capture_time == photo.capture_time  # 대표 = HEIC


def test_live_photo_representative_time_is_photo():
    # 영상 쪽 시각이 약간 달라도 대표는 HEIC 시각
    photo = mf("X.heic", DateTimeOriginal="2024:07:15 14:30:22")
    video = mf("X.mov", CreationDate="2024:07:15 14:30:25+09:00")
    units = pair_live_photos([photo, video])
    pair = next(u for u in units if len(u.files) == 2)
    assert pair.capture_time == dt.datetime(2024, 7, 15, 14, 30, 22)


def test_edited_livephoto_stem_mismatch_not_paired():
    # 편집본 IMG_E1234 는 stem 이 달라 M1 stem매칭에선 안 묶임(알려진 한계)
    photo = mf("IMG_E1.heic", DateTimeOriginal="2024:07:15 14:30:22")
    video = mf("IMG_1.mov", CreationDate="2024:07:15 14:30:22+09:00")
    units = pair_live_photos([photo, video])
    assert all(len(u.files) == 1 for u in units)


def test_pair_not_split_across_event_boundary():
    # 짝의 사진/영상 시각차가 갭을 넘겨도 한 덩어리 → 같은 이벤트
    photo = mf("P.heic", DateTimeOriginal="2024:07:15 14:30:22")
    video = mf("P.mov", CreationDate="2024:07:15 23:30:22+09:00")  # 9h 차
    units = pair_live_photos([photo, video])
    events = split_events(units, gap_hours=4)
    assert len(events) == 1
    assert events[0].photo_count == 2


# ─── [T2] 이벤트 분할 / 안정키 ────────────────────────────────────────────────

def _solo(name, time_str):
    m = mf(name, DateTimeOriginal=time_str)
    return Unit(files=[m], capture_time=m.capture_time, is_quarantined=False)


def test_split_gap_exactly_4h_no_boundary():
    a = _solo("a.heic", "2024:07:15 10:00:00")
    b = _solo("b.heic", "2024:07:15 14:00:00")  # 정확히 4h → 경계 아님
    events = split_events([a, b], gap_hours=4)
    assert len(events) == 1


def test_split_gap_over_4h_creates_boundary():
    a = _solo("a.heic", "2024:07:15 10:00:00")
    b = _solo("b.heic", "2024:07:15 14:00:01")  # 4h+1s → 경계
    events = split_events([a, b], gap_hours=4)
    assert len(events) == 2


def test_split_gap_under_4h_same_event():
    a = _solo("a.heic", "2024:07:15 10:00:00")
    b = _solo("b.heic", "2024:07:15 13:59:59")
    events = split_events([a, b], gap_hours=4)
    assert len(events) == 1


def test_split_single_unit():
    events = split_events([_solo("a.heic", "2024:07:15 10:00:00")], gap_hours=4)
    assert len(events) == 1 and events[0].photo_count == 1


def test_split_empty():
    assert split_events([], gap_hours=4) == []


def test_split_multi_month_event_uses_first_month():
    # 연말 여행: 12-31 ~ 01-01, 4h 내 연속 → 한 이벤트, 첫 사진 월(12) 기준
    a = _solo("a.heic", "2024:12:31 23:00:00")
    b = _solo("b.heic", "2025:01:01 01:00:00")
    events = split_events([a, b], gap_hours=4)
    assert len(events) == 1
    assert events[0].key.startswith("2024-12")


def test_event_key_stable_when_photo_added():
    # 사진 추가해도 기존 이벤트 시작시각 불변 → 키 불변(D10, N2)
    base = [_solo("a.heic", "2024:07:15 10:00:00"),
            _solo("b.heic", "2024:07:15 11:00:00")]
    key_before = split_events(base, 4)[0].key
    # 더 늦은 사진 추가(같은 이벤트)
    base.append(_solo("c.heic", "2024:07:15 12:00:00"))
    key_after = split_events(base, 4)[0].key
    assert key_before == key_after == "2024-07-15_100000"


def test_event_key_deterministic():
    assert event_key(dt.datetime(2024, 7, 15, 14, 30, 22)) == "2024-07-15_143022"


# ─── [T5] 새 이름 / 충돌 / 결정성 ─────────────────────────────────────────────

def test_basename_format():
    assert base_name_for(dt.datetime(2024, 7, 15, 14, 30, 22)) == "2024-07-15_143022"


def test_new_filename_basic():
    f = mf("IMG_1234.heic", DateTimeOriginal="2024:07:15 14:30:22")
    name = new_filename(f, f.capture_time, taken=set())
    assert name == "2024-07-15_143022.heic"


def test_new_filename_same_second_collision_uses_original_suffix():
    a = mf("IMG_1.heic", DateTimeOriginal="2024:07:15 14:30:22")
    b = mf("IMG_2.heic", DateTimeOriginal="2024:07:15 14:30:22")
    taken = set()
    n1 = new_filename(a, a.capture_time, taken); taken.add(n1)
    n2 = new_filename(b, b.capture_time, taken); taken.add(n2)
    assert n1 == "2024-07-15_143022.heic"
    assert n2 == "2024-07-15_143022_IMG_2.heic"  # 원본명 접미사(D1)


def test_new_filename_deterministic_same_input():
    a = mf("IMG_1.heic", DateTimeOriginal="2024:07:15 14:30:22")
    assert new_filename(a, a.capture_time, set()) == \
           new_filename(a, a.capture_time, set())


def test_keep_original_name_option():
    f = mf("IMG_9.heic", DateTimeOriginal="2024:07:15 14:30:22")
    name = new_filename(f, f.capture_time, set(), keep_original=True)
    assert name == "2024-07-15_143022_IMG_9.heic"


def test_sanitize_strips_unsafe_chars():
    assert "/" not in sanitize_component("a/b:c")
    assert sanitize_component("a/b") == "a_b"


def test_sanitize_length_cap():
    assert len(sanitize_component("x" * 500)) <= 120


# ─── build_plan: 짝 동일 basename / 격리 이름유지 ─────────────────────────────

def test_plan_pair_shares_basename():
    photo = mf("IMG_1.heic", DateTimeOriginal="2024:07:15 14:30:22")
    video = mf("IMG_1.mov", CreationDate="2024:07:15 14:30:22+09:00")
    units = pair_live_photos([photo, video])
    events = split_events(units, 4)
    plan = build_plan(events, [])
    stems = {p.dst.stem for p in plan}
    assert len(stems) == 1  # 짝이 같은 basename 공유


def test_plan_quarantine_keeps_original_name():
    q = mf("weird_screenshot.png")  # 시각없음 → 격리
    units = pair_live_photos([q])
    quarantined = [u for u in units if u.is_quarantined]
    plan = build_plan([], quarantined)
    assert len(plan) == 1
    assert plan[0].quarantined
    assert plan[0].dst == Path(QUARANTINE_DIR) / "weird_screenshot.png"


# ─── 쓰레기(이름패턴) 격리 ────────────────────────────────────────────────────

def test_is_junk_patterns():
    assert is_junk("스크린샷 2021-04-28.jpeg")
    assert is_junk("Screenshot 2024.png")
    assert is_junk("배경화면_1.jpeg")
    assert is_junk("MBTI_20200517_kr.jpeg")
    assert is_junk("제목_없는_아트워크 (2).jpeg")
    assert is_junk("Resized_20230406.jpeg")


def test_is_junk_matches_nfd_korean():
    # macOS 가 저장하는 NFD(자모분리) 형태도 매칭돼야 함
    nfd = unicodedata.normalize("NFD", "배경화면_1.jpeg")
    assert nfd != "배경화면_1.jpeg"  # 실제로 다른 바이트열
    assert is_junk(nfd)
    assert is_junk(unicodedata.normalize("NFD", "제목_없는_아트워크.jpeg"))


def test_real_photos_not_junk():
    # EXIF 벗겨진 진짜 사진은 이름으로 안 걸린다
    assert not is_junk("IMG_5483.jpeg")
    assert not is_junk("__1002 함창수277605.jpeg")
    assert not is_junk("___DSC01076(보정).jpeg")


def test_junk_pulled_out_real_photos_kept(tmp_path, monkeypatch):
    # 진짜 사진 2장(EXIF 없어도) + 스크린샷 1장 → 사진은 이벤트, 스크린샷은 _기타
    src = tmp_path / "src"
    src.mkdir()
    def fake(s):
        a = MediaFile(src=src / "함창수1.jpeg", meta={"DateTimeOriginal": "2024:07:15 10:00:00"})
        b = MediaFile(src=src / "함창수2.jpeg", meta={"DateTimeOriginal": "2024:07:15 10:05:00"})
        shot = MediaFile(src=src / "스크린샷.png", meta={"DateTimeOriginal": "2024:07:15 10:02:00"})
        for m in (a, b, shot):
            pick_capture_time(m)
        return [a, b, shot]
    monkeypatch.setattr(o, "load_media", fake)
    events, quarantined, junk, plan = plan_from_source(src, 4, False)
    assert len(events) == 1 and events[0].photo_count == 2  # EXIF 없는 진짜 사진도 이벤트로
    assert sum(len(u.files) for u in junk) == 1
    shot_item = next(p for p in plan if p.src.name == "스크린샷.png")
    assert shot_item.dst == Path(JUNK_DIR) / "스크린샷.png"  # 원래 이름 유지


def test_junk_does_not_break_livephoto_pair():
    # 정상 이름의 라이브포토 짝은 쓰레기로 안 걸리고 묶임
    photo = mf("IMG_1.heic", DateTimeOriginal="2024:07:15 14:30:22")
    video = mf("IMG_1.mov", CreationDate="2024:07:15 14:30:22+09:00")
    units = pair_live_photos([photo, video])
    assert any(len(u.files) == 2 for u in units)
    assert not any(u.primary.is_junk for u in units)


# ─── [T1] 복사 검증 / 손상 탐지 ───────────────────────────────────────────────

def test_copy_basic_and_verify(tmp_path):
    src = write_jpg(tmp_path / "src" / "a.jpg", b"hello world")
    dst = tmp_path / "dst" / "a.jpg"
    result, sha, size = copy_one(src, dst, verify=True)
    assert result == CopyResult.COPIED
    assert dst.read_bytes() == b"hello world"
    assert sha == hashlib.sha256(b"hello world").hexdigest()


def test_copy_no_tmp_left_behind(tmp_path):
    src = write_jpg(tmp_path / "src" / "a.jpg")
    dst = tmp_path / "dst" / "a.jpg"
    copy_one(src, dst, verify=True)
    assert not (tmp_path / "dst" / "a.jpg.tmp").exists()


# ─── 충돌 규칙: skip / 둘다보존 ───────────────────────────────────────────────

def test_collision_same_name_same_size_skips(tmp_path):
    src = write_jpg(tmp_path / "src" / "a.jpg", b"same")
    dst = tmp_path / "dst" / "a.jpg"
    write_jpg(dst, b"same")  # 이미 존재, 같은 크기
    result, _, _ = copy_one(src, dst, verify=True)
    assert result == CopyResult.SKIPPED


def test_collision_diff_size_preserves_both(tmp_path):
    src = write_jpg(tmp_path / "src" / "a.jpg", b"new-bigger-content")
    dst = tmp_path / "dst" / "a.jpg"
    write_jpg(dst, b"old")  # 다른 크기
    result, _, _ = copy_one(src, dst, verify=True)
    assert result == CopyResult.COPIED
    # 원본 목적지 보존 + 새 파일 별도
    assert dst.read_bytes() == b"old"
    others = list((tmp_path / "dst").glob("a_*.jpg"))
    assert len(others) == 1 and others[0].read_bytes() == b"new-bigger-content"


# ─── 4GB 격리(exFAT) ─────────────────────────────────────────────────────────

def test_exfat_over_4gb_quarantined(tmp_path, monkeypatch):
    src = write_jpg(tmp_path / "src" / "big.mov", b"x")
    # stat 크기를 4GB+ 로 가장
    real_stat = Path.stat
    def fake_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self == src:
            class S:
                st_size = o.EXFAT_MAX_BYTES + 1
                st_mtime = st.st_mtime
            return S()
        return st
    monkeypatch.setattr(Path, "stat", fake_stat)
    result, _, _ = copy_one(src, tmp_path / "dst" / "big.mov",
                            verify=True, is_exfat=True)
    assert result == CopyResult.QUARANTINED_SIZE


# ─── ★★★ 절대필수 3종 ────────────────────────────────────────────────────────

def _build_dummy_source(tmp_path) -> Path:
    """exiftool 없이 plan_from_source 를 돌리기 위해 load_media 를 가로챌
    수 있도록, 실제 파일 + 메타를 함께 만든다."""
    src = tmp_path / "src"
    src.mkdir()
    write_jpg(src / "a.heic", b"aaa")
    write_jpg(src / "b.heic", b"bbbbb")
    return src


def _fake_media(src: Path):
    a = MediaFile(src=src / "a.heic", meta={"DateTimeOriginal": "2024:07:15 10:00:00"})
    b = MediaFile(src=src / "b.heic", meta={"DateTimeOriginal": "2024:07:15 11:00:00"})
    pick_capture_time(a); pick_capture_time(b)
    return [a, b]


def test_STAR_dryrun_writes_nothing(tmp_path, monkeypatch, capsys):
    """★ dry-run 후 목적지 파일수 == 0."""
    src = _build_dummy_source(tmp_path)
    dst = tmp_path / "dst"
    monkeypatch.setattr(o, "load_media", lambda s: _fake_media(src))
    monkeypatch.setattr(o, "detect_exfat", lambda p: False)
    rc = o.main([str(src), str(dst), "--dry-run"])
    assert rc == 0
    # 목적지엔 아무 사진도 없어야 함(맨페스트조차 dry-run 에선 안 만듦)
    copied = [p for p in dst.rglob("*") if p.is_file() and p.suffix == ".heic"]
    assert copied == []


def test_STAR_idempotent_no_dup(tmp_path, monkeypatch):
    """★ 같은 입력 2회 → 중복 0, 결과 동일."""
    src = _build_dummy_source(tmp_path)
    dst = tmp_path / "dst"
    monkeypatch.setattr(o, "load_media", lambda s: _fake_media(src))
    monkeypatch.setattr(o, "detect_exfat", lambda p: False)

    o.main([str(src), str(dst), "--copy"])
    first = sorted(p.relative_to(dst) for p in dst.rglob("*")
                   if p.is_file() and p.suffix == ".heic")
    o.main([str(src), str(dst), "--copy"])  # 재실행
    second = sorted(p.relative_to(dst) for p in dst.rglob("*")
                    if p.is_file() and p.suffix == ".heic")
    assert first == second  # 경로 동일
    assert len(second) == 2  # 중복 복사 없음

    # 맨페스트 2회차는 전부 skip 으로 기록
    conn = sqlite3.connect(dst / o.MANIFEST_NAME)
    runs = conn.execute("SELECT copied, skipped FROM runs ORDER BY id").fetchall()
    conn.close()
    assert runs[0] == (2, 0)   # 1회차: 2 복사
    assert runs[1] == (0, 2)   # 2회차: 2 skip


def test_STAR_originals_untouched(tmp_path, monkeypatch):
    """★ 실행 후 모든 소스 파일 내용·mtime 불변."""
    src = _build_dummy_source(tmp_path)
    dst = tmp_path / "dst"
    files = list(src.glob("*.heic"))
    before = {f: (f.read_bytes(), f.stat().st_mtime) for f in files}

    monkeypatch.setattr(o, "load_media", lambda s: _fake_media(src))
    monkeypatch.setattr(o, "detect_exfat", lambda p: False)
    o.main([str(src), str(dst), "--copy"])

    for f in files:
        content, mtime = before[f]
        assert f.read_bytes() == content
        assert f.stat().st_mtime == mtime


# ─── 여유공간 부족 ────────────────────────────────────────────────────────────

def test_insufficient_space_aborts(tmp_path, monkeypatch, capsys):
    src = _build_dummy_source(tmp_path)
    dst = tmp_path / "dst"
    monkeypatch.setattr(o, "load_media", lambda s: _fake_media(src))
    monkeypatch.setattr(o, "detect_exfat", lambda p: False)
    monkeypatch.setattr(o, "free_bytes", lambda p: 0)  # 여유 0
    rc = o.main([str(src), str(dst), "--copy"])
    assert rc == 1
    assert "부족" in capsys.readouterr().err
    # 복사 안 됨
    assert [p for p in dst.rglob("*.heic")] == []
