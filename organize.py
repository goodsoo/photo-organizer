#!/usr/bin/env python3
"""사진 이벤트 자동 정리기 — M1 (Approach A).

파일 더미 입력 → 촬영시각 추출 → 라이브포토 짝 묶기 → 4h 갭 이벤트 분할
→ 촬영시각 기반 새 이름 → dry-run 계획 → (--copy) 원자적 복사+sha256 검증
→ SQLite 맨페스트 기록.

DESIGN.md 의 확정 결정(D1~D11, N1~N5, C1~C3)에 1:1 대응. 순수로직과 IO 를
엄격히 분리해 디스크 없이 단위 테스트가 가능하다(D8).

    python3 organize.py <src> <dst> --dry-run
    python3 organize.py <src> <dst> --copy
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

# ─────────────────────────────────────────────────────────────────────────────
# 상수 / 분류
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1
DEFAULT_GAP_HOURS = 4.0
EXFAT_MAX_BYTES = 4 * 1024 * 1024 * 1024 - 1  # 4GiB - 1, exFAT 단일파일 한계(D5)

PHOTO_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff",
              ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf",
              ".rw2", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".3gp"}
LIVE_VIDEO_EXTS = {".mov", ".mp4"}  # 라이브포토 짝의 영상 쪽

QUARANTINE_DIR = "_분류안됨"
JUNK_DIR = "_기타"   # 스크린샷·배경화면·아트워크 등 (시각은 있으나 사진 이벤트 아님)

# 쓸데없는 이미지로 보는 파일명 패턴(소문자 부분일치). 사용자가 추가/수정 가능.
# 진짜 사진(함창수 스튜디오·DSC 보정본·받은 사진)은 여기 안 걸리고 이벤트로 간다.
JUNK_NAME_PATTERNS = [
    "스크린샷", "screenshot", "screen shot",
    "배경화면", "wallpaper",
    "mbti",
    "아트워크", "artwork",
    "resized",
]

# exiftool 의 더티값 패턴(N5): 전무·부분·0000 등
_DIRTY_DATE_PREFIXES = ("0000:00:00", "0000-00-00", "    :  :  ")

def is_junk(filename: str) -> bool:
    """파일명이 쓸데없는 이미지(스크린샷·배경화면·MBTI·아트워크·Resized) 패턴에
    걸리는지. 이름 기반 휴리스틱 — 진짜 사진은 이런 이름을 거의 안 쓴다.

    카메라 EXIF 유무로 가르면 EXIF 벗겨진 진짜 사진(보정본·받은 사진)까지
    빠지므로, 사용자 선택으로 이름 패턴만 격리한다(JUNK_NAME_PATTERNS).

    macOS 는 한글 파일명을 NFD(자모분리)로 저장하므로 NFC 로 정규화 후 비교한다
    — 안 하면 한글 패턴이 절대 매칭되지 않는다.
    """
    low = unicodedata.normalize("NFC", filename).lower()
    return any(unicodedata.normalize("NFC", p) in low for p in JUNK_NAME_PATTERNS)


# DateTimeOriginal 등의 기본 포맷: "2024:07:15 14:30:22"
_DT_RE = re.compile(
    r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.\d+)?"                       # 소수 초 무시
    r"(?:\s*([+\-]\d{2}:?\d{2}|Z))?"    # 선택적 타임존 오프셋
)


# ─────────────────────────────────────────────────────────────────────────────
# 데이터 모델
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class MediaFile:
    """입력 파일 1개. exiftool 메타 + 추출 결과."""
    src: Path
    meta: dict
    capture_time: Optional[dt.datetime] = None   # naive 로컬시각 (None=격리)
    time_source: Optional[str] = None             # 어떤 태그에서 왔는지
    quarantine_reason: Optional[str] = None       # 격리 사유 (None=정상)

    @property
    def is_quarantined(self) -> bool:
        return self.capture_time is None

    @property
    def ext(self) -> str:
        return self.src.suffix.lower()

    @property
    def stem(self) -> str:
        return self.src.stem

    @property
    def is_junk(self) -> bool:
        return is_junk(self.src.name)


@dataclasses.dataclass
class Unit:
    """이동의 최소 단위. 단일 파일이거나 라이브포토 짝(여러 파일)."""
    files: list[MediaFile]              # 대표파일이 [0]
    capture_time: Optional[dt.datetime]
    is_quarantined: bool
    quarantine_reason: Optional[str] = None

    @property
    def primary(self) -> MediaFile:
        return self.files[0]


@dataclasses.dataclass
class Event:
    """4h 갭으로 분리된 이벤트 묶음."""
    key: str                            # YYYY-MM_HHMMSS 안정키 (D10)
    units: list[Unit]
    start: dt.datetime
    end: dt.datetime

    @property
    def photo_count(self) -> int:
        return sum(len(u.files) for u in self.units)


@dataclasses.dataclass
class PlanItem:
    """소스파일 → 목적지경로 1:1 매핑(dry-run/복사 공용)."""
    src: Path
    dst: Path                           # 최종 목적지 (충돌 해소 후)
    event_key: str
    capture_time: Optional[dt.datetime]
    quarantined: bool
    reason: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# [2] 촬영시각 추출  pick_capture_time()  — 순수 (T3, D7, N1, N5)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_exif_dt(value) -> Optional[tuple[dt.datetime, bool]]:
    """exiftool 날짜문자열 → (naive 로컬 datetime, has_offset). 더티/파싱불가는 None."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    for bad in _DIRTY_DATE_PREFIXES:
        if s.startswith(bad):
            return None
    m = _DT_RE.match(s)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(m.group(i)) for i in range(1, 7))
    try:
        # 타임존이 있어도 M1 은 "찍힌 그대로의 로컬시각"을 쓴다(naive).
        parsed = dt.datetime(y, mo, d, h, mi, se)
    except ValueError:
        return None  # 13월·32일 등
    has_offset = m.group(7) is not None
    return parsed, has_offset


def pick_capture_time(media: MediaFile) -> MediaFile:
    """파일종류별 우선순위로 촬영 로컬시각을 정함. 실패시 격리표시.

    사진:  DateTimeOriginal → CreateDate
    영상:  CreationDate(로컬오프셋 포함 가능) → (UTC뿐인 태그만 있으면 격리, N1)
    더티/전무 → 격리(N5). media 를 제자리 갱신해 반환.
    """
    meta = media.meta
    ext = media.ext

    if ext in VIDEO_EXTS:
        # 영상: CreationDate 가 아이폰 로컬시각을 담음. 이게 없고 UTC계열만
        # 있으면 9h 어긋나 이벤트분할을 오염시키므로 격리(N1).
        candidates = [("CreationDate", meta.get("CreationDate"))]
        for tag, val in candidates:
            parsed = _parse_exif_dt(val)
            if parsed:
                media.capture_time, media.time_source = parsed[0], tag
                return media
        # UTC 계열만 있는지 확인 → 있으면 "UTC뿐" 격리, 아예 없으면 "시각없음"
        utc_only = any(_parse_exif_dt(meta.get(t)) for t in
                       ("MediaCreateDate", "CreateDate", "TrackCreateDate"))
        media.quarantine_reason = "video_utc_only" if utc_only else "no_capture_time"
        return media

    # 사진(및 알 수 없는 확장자도 사진 취급)
    for tag in ("DateTimeOriginal", "CreateDate", "ModifyDate"):
        # ModifyDate 는 마지막 폴백 — 신뢰낮지만 DateTimeOriginal/CreateDate 가
        # 둘 다 없을 때만. (대개 둘 중 하나는 있음)
        if tag == "ModifyDate":
            break  # M1: ModifyDate 폴백 안 함 — 파일시각 추정 금지 원칙(전제2)
        parsed = _parse_exif_dt(meta.get(tag))
        if parsed:
            media.capture_time, media.time_source = parsed[0], tag
            return media

    media.quarantine_reason = "no_capture_time"
    return media


# ─────────────────────────────────────────────────────────────────────────────
# [3] 라이브포토 짝 묶기  pair_live_photos()  — 순수 (T4, D4)
# ─────────────────────────────────────────────────────────────────────────────

def pair_live_photos(files: list[MediaFile]) -> list[Unit]:
    """같은 stem 의 사진+영상을 한 Unit 으로 묶는다(M1 = stem 매칭).

    대표시각 = 사진(HEIC) 쪽 촬영시각. 짝은 이후 절대 갈라지지 않는다.
    격리 파일은 짝 묶지 않고 각자 단독 Unit. 같은 stem 에 사진이 여러 장이면
    매칭하지 않고 모두 단독(애매하면 묶지 않음 — 손실/오분류 회피).
    """
    by_stem: dict[str, list[MediaFile]] = defaultdict(list)
    for f in files:
        by_stem[f.stem].append(f)

    units: list[Unit] = []
    for stem, group in by_stem.items():
        photos = [f for f in group if f.ext in PHOTO_EXTS and not f.is_quarantined]
        videos = [f for f in group if f.ext in LIVE_VIDEO_EXTS]

        # 라이브포토 조건: 사진 정확히 1장 + 영상 1장 이상, 사진이 시각 보유
        if len(photos) == 1 and videos:
            photo = photos[0]
            paired = [photo] + videos
            paired_set = set(id(x) for x in paired)
            units.append(Unit(
                files=paired,
                capture_time=photo.capture_time,
                is_quarantined=False,
            ))
            # 묶이지 않은 나머지(여분 사진 등)는 아래서 단독 처리
            for f in group:
                if id(f) not in paired_set:
                    units.append(_solo_unit(f))
        else:
            for f in group:
                units.append(_solo_unit(f))
    return units


def _solo_unit(f: MediaFile) -> Unit:
    return Unit(
        files=[f],
        capture_time=f.capture_time,
        is_quarantined=f.is_quarantined,
        quarantine_reason=f.quarantine_reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# [4] 이벤트 분할  split_events()  — 순수 (T2, D10, N2)
# ─────────────────────────────────────────────────────────────────────────────

def event_key(start: dt.datetime) -> str:
    """이벤트 시작시각 기반 안정키. 재실행/사진추가에도 불변(D10, N2).

    형식: YYYY-MM-DD_HHMMSS  (이벤트 첫 사진의 날짜·시각)
    예) 2024-07-15_143022
    날짜에 일까지 넣어 사람이 읽기 쉽게 함 — 4h 갭으로 묶인 한 덩어리라
    이름에 시작일을 넣어도 묶임은 그대로다(두 달 걸친 여행도 한 폴더).
    """
    return f"{start:%Y-%m-%d}_{start:%H%M%S}"


def split_events(units: list[Unit], gap_hours: float = DEFAULT_GAP_HOURS) -> list[Event]:
    """대표시각 정렬 → 인접 갭이 gap_hours 초과하면 새 이벤트(경계).

    격리 Unit 은 이벤트에 넣지 않고 제외(호출측에서 별도 처리).
    """
    timed = sorted((u for u in units if not u.is_quarantined),
                   key=lambda u: u.capture_time)
    if not timed:
        return []

    gap = dt.timedelta(hours=gap_hours)
    events: list[Event] = []
    bucket: list[Unit] = [timed[0]]

    for prev, cur in zip(timed, timed[1:]):
        if cur.capture_time - prev.capture_time > gap:
            events.append(_make_event(bucket))
            bucket = [cur]
        else:
            bucket.append(cur)
    events.append(_make_event(bucket))
    return events


def _make_event(bucket: list[Unit]) -> Event:
    start = bucket[0].capture_time
    end = bucket[-1].capture_time
    return Event(key=event_key(start), units=list(bucket), start=start, end=end)


# ─────────────────────────────────────────────────────────────────────────────
# [5] 새 이름  new_filename()  — 순수 (T5, D1, C2)
# ─────────────────────────────────────────────────────────────────────────────

_UNSAFE_CHARS = re.compile(r'[/\\\x00-\x1f<>:"|?*]')
_MAX_STEM = 120  # 파일명 길이 상한(C2)


def sanitize_component(name: str) -> str:
    """경로구분자·제어문자 치환, 길이 제한(C2 — 신뢰불가 원본명 새니타이즈)."""
    cleaned = _UNSAFE_CHARS.sub("_", name)
    cleaned = cleaned.strip(" .") or "_"
    if len(cleaned) > _MAX_STEM:
        cleaned = cleaned[:_MAX_STEM]
    return cleaned


def base_name_for(capture_time: dt.datetime) -> str:
    """촬영시각 기반 기본 basename(확장자 제외): YYYY-MM-DD_HHMMSS."""
    return f"{capture_time:%Y-%m-%d_%H%M%S}"


def new_filename(unit_file: MediaFile, capture_time: dt.datetime,
                 taken: set[str], keep_original: bool = False) -> str:
    """파일 1개의 새 파일명(확장자 포함)을 결정. 같은-초 충돌은 원본명 접미사(D1).

    결정적: 같은 입력 → 같은 이름(멱등). `taken` 은 이미 배정된 basename 집합
    (확장자 포함, 같은 이벤트 폴더 내). 충돌시 원본 stem 을 접미사로 붙인다.
    """
    ext = unit_file.ext
    base = base_name_for(capture_time)
    orig = sanitize_component(unit_file.stem)

    if keep_original:
        candidate = f"{base}_{orig}{ext}"
    else:
        candidate = f"{base}{ext}"
        if candidate in taken:
            # 같은 초 충돌 → 원본 파일명 접미사(순번 아님, 추적가능·결정적)
            candidate = f"{base}_{orig}{ext}"

    # 그래도 충돌하면(동일 원본명까지 겹침) 원본 상대경로 해시 접미사
    if candidate in taken:
        h = hashlib.sha1(str(unit_file.src).encode("utf-8")).hexdigest()[:8]
        candidate = f"{base}_{orig}_{h}{ext}"
    return candidate


def sideline_filename(unit_file: MediaFile, taken: set[str]) -> str:
    """격리/비카메라 파일명: 원래 이름 유지. 충돌시 해시 접미사.

    이벤트가 아니므로 촬영시각 기반 새 이름을 붙이지 않고 사람이 알아보던
    원래 이름을 그대로 둔다(격리·비카메라 공통).
    """
    name = sanitize_component(unit_file.stem) + unit_file.ext
    if name in taken:
        h = hashlib.sha1(str(unit_file.src).encode("utf-8")).hexdigest()[:8]
        name = f"{sanitize_component(unit_file.stem)}_{h}{unit_file.ext}"
    return name


# ─────────────────────────────────────────────────────────────────────────────
# 계획 합성  build_plan()  — 순수 (이벤트 → PlanItem 목록)
# ─────────────────────────────────────────────────────────────────────────────

def build_plan(events: list[Event], quarantined: list[Unit],
               junk: Optional[list[Unit]] = None,
               keep_original: bool = False) -> list[PlanItem]:
    """이벤트/격리/기타 Unit → 최종 PlanItem 목록. 짝은 같은 basename 공유(D4).

    각 이벤트 폴더 내에서 이름 충돌을 결정적으로 해소. 격리는 _분류안됨/, 쓰레기는
    _기타/ 로 (둘 다 원래 이름 유지).
    """
    plan: list[PlanItem] = []

    for ev in events:
        taken: set[str] = set()
        # Unit 정렬: 시각→원본경로, 결정성 보장(멱등)
        for unit in sorted(ev.units, key=lambda u: (u.capture_time, str(u.primary.src))):
            ct = unit.capture_time
            # 짝은 동일 basename 공유: 대표파일로 basename 정한 뒤 확장자만 바꿈
            primary = unit.primary
            primary_name = new_filename(primary, ct, taken, keep_original)
            taken.add(primary_name)
            shared_stem = primary_name.rsplit(".", 1)[0]

            for f in unit.files:
                if f is primary:
                    name = primary_name
                else:
                    # 짝 구성원: 같은 stem + 자기 확장자
                    name = f"{shared_stem}{f.ext}"
                    if name in taken:
                        h = hashlib.sha1(str(f.src).encode()).hexdigest()[:8]
                        name = f"{shared_stem}_{h}{f.ext}"
                    taken.add(name)
                plan.append(PlanItem(
                    src=f.src,
                    dst=Path(ev.key) / name,
                    event_key=ev.key,
                    capture_time=ct,
                    quarantined=False,
                ))

    # 격리 + 기타 (둘 다 원래 이름 유지, 별도 폴더)
    for units, dirname in ((quarantined, QUARANTINE_DIR),
                           (junk or [], JUNK_DIR)):
        taken: set[str] = set()
        for unit in sorted(units, key=lambda u: str(u.primary.src)):
            for f in unit.files:
                name = sideline_filename(f, taken)
                taken.add(name)
                plan.append(PlanItem(
                    src=f.src,
                    dst=Path(dirname) / name,
                    event_key=dirname,
                    capture_time=f.capture_time,  # 기타도 시각은 있음
                    quarantined=True,
                    reason=f.quarantine_reason or
                           (None if dirname == QUARANTINE_DIR else "junk"),
                ))
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# [1] exiftool 배치 추출  — IO (D11 에러처리)
# ─────────────────────────────────────────────────────────────────────────────

def run_exiftool(src: Path) -> list[dict]:
    """`exiftool -json -r <src>` 1회 배치 호출(파일별 호출 금지).

    Korean/유니코드 파일명 안전하게 처리. 실패시 명시적 예외(D11).
    """
    if shutil.which("exiftool") is None:
        raise RuntimeError(
            "exiftool 이 설치돼 있지 않습니다. `brew install exiftool` 후 다시 실행하세요."
        )
    cmd = ["exiftool", "-json", "-r", "-charset", "filename=utf8",
           "-api", "largefilesupport=1", str(src)]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as e:
        raise RuntimeError(f"exiftool 실행 실패: {e}") from e
    if proc.returncode not in (0, 1):  # 1 = 일부 파일 경고(허용)
        raise RuntimeError(
            f"exiftool 비정상 종료(code={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')[:500]}"
        )
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"exiftool JSON 파싱 실패: {e}") from e


def get_exiftool_version() -> str:
    try:
        return subprocess.run(["exiftool", "-ver"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def load_media(src: Path) -> list[MediaFile]:
    """exiftool 출력 → 시각추출까지 끝낸 MediaFile 목록."""
    records = run_exiftool(src)
    media: list[MediaFile] = []
    for r in records:
        path = r.get("SourceFile")
        if not path:
            continue
        p = Path(path)
        if p.is_dir():
            continue
        mf = MediaFile(src=p, meta=r)
        pick_capture_time(mf)
        media.append(mf)
    return media


# ─────────────────────────────────────────────────────────────────────────────
# [6] 여유공간 / 파일시스템  — IO (D5, D6)
# ─────────────────────────────────────────────────────────────────────────────

def total_source_bytes(plan: list[PlanItem]) -> int:
    total = 0
    for item in plan:
        try:
            total += item.src.stat().st_size
        except OSError:
            pass
    return total


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def detect_exfat(path: Path) -> bool:
    """목적지가 exFAT 인지 추정(macOS `diskutil`). 실패시 False(보수적)."""
    try:
        out = subprocess.run(["diskutil", "info", str(path)],
                             capture_output=True, text=True, check=False).stdout
        return "exfat" in out.lower()
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# [7] 원자적 복사 + sha256 검증  — IO (T1, D2, D3, N4)
# ─────────────────────────────────────────────────────────────────────────────

class CopyResult:
    SKIPPED = "skipped"       # 같은 이름+같은 크기 → 동일파일
    COPIED = "copied"
    PRESERVED = "preserved"   # 다른 크기 → 접미사 붙여 둘다 보존
    QUARANTINED_SIZE = "quarantined_4gb"
    FAILED = "failed"


def _sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def copy_one(src: Path, dst: Path, *, verify: bool = True,
             is_exfat: bool = False) -> tuple[str, Optional[str], int]:
    """파일 1개를 원자적으로 복사. (결과상태, sha256, size) 반환.

    .tmp 에 쓰며 sha256 동시계산 → fsync → 되읽어 검증 → os.rename(원자적).
    충돌: 같은이름+같은크기 skip / 다른크기 접미사 보존(덮어쓰기 절대 금지).
    exFAT 에서 4GB 초과는 호출측이 격리로 보냄(여기선 방어적 체크).
    """
    size = src.stat().st_size
    if is_exfat and size > EXFAT_MAX_BYTES:
        return (CopyResult.QUARANTINED_SIZE, None, size)

    dst = _resolve_collision(src, dst, size, is_exfat)
    if dst is None:
        return (CopyResult.SKIPPED, None, size)

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")

    h = hashlib.sha256()
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            for block in iter(lambda: fin.read(1 << 20), b""):
                fout.write(block)
                h.update(block)
            fout.flush()
            os.fsync(fout.fileno())   # N4: 디스크까지 내려쓰기 보장
        digest = h.hexdigest()

        if verify:
            # 되읽어 sha256 재계산 → 비트로테/부분쓰기 탐지(D3)
            if _sha256_of(tmp) != digest:
                tmp.unlink(missing_ok=True)
                return (CopyResult.FAILED, None, size)
            if tmp.stat().st_size != size:
                tmp.unlink(missing_ok=True)
                return (CopyResult.FAILED, None, size)

        os.replace(tmp, dst)          # 원자적 rename(D2)
        shutil.copystat(src, dst, follow_symlinks=False)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return (CopyResult.FAILED, None, size)

    return (CopyResult.COPIED, digest, size)


def _resolve_collision(src: Path, dst: Path, size: int,
                       is_exfat: bool) -> Optional[Path]:
    """목적지 충돌 해소. 동일파일이면 None(skip), 다르면 접미사 경로 반환.

    같은이름+같은크기 → 동일파일로 보고 skip(멱등). 다른크기 → 접미사 보존.
    exFAT 은 대소문자 무시 비교(D5).
    """
    if not _exists_casecmp(dst, is_exfat):
        return dst
    existing = _find_existing(dst, is_exfat)
    if existing and existing.stat().st_size == size:
        return None  # 동일 파일 → skip
    # 다른 크기 → 접미사로 둘 다 보존
    stem, suffix = dst.stem, dst.suffix
    h = hashlib.sha1(str(src).encode()).hexdigest()[:8]
    candidate = dst.with_name(f"{stem}_{h}{suffix}")
    n = 1
    while _exists_casecmp(candidate, is_exfat):
        candidate = dst.with_name(f"{stem}_{h}_{n}{suffix}")
        n += 1
    return candidate


def _exists_casecmp(path: Path, is_exfat: bool) -> bool:
    if path.exists():
        return True
    if is_exfat and path.parent.exists():
        low = path.name.lower()
        return any(c.name.lower() == low for c in path.parent.iterdir())
    return False


def _find_existing(path: Path, is_exfat: bool) -> Optional[Path]:
    if path.exists():
        return path
    if is_exfat and path.parent.exists():
        low = path.name.lower()
        for c in path.parent.iterdir():
            if c.name.lower() == low:
                return c
    return None


# ─────────────────────────────────────────────────────────────────────────────
# [8] SQLite 맨페스트  — IO (T6, D9, N3, C3)
# ─────────────────────────────────────────────────────────────────────────────

MANIFEST_NAME = "manifest.sqlite3"


def open_manifest(dst_root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(dst_root / MANIFEST_NAME)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            src_root TEXT NOT NULL,
            dst_root TEXT NOT NULL,
            exiftool_version TEXT,
            schema_version INTEGER NOT NULL,
            gap_hours REAL NOT NULL,
            copied INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            quarantined INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id),
            src_path TEXT NOT NULL,
            dst_path TEXT NOT NULL,
            event_key TEXT NOT NULL,
            capture_time TEXT,
            sha256 TEXT,
            size INTEGER,
            mtime REAL,
            result TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
        CREATE INDEX IF NOT EXISTS idx_files_event ON files(event_key);
    """)
    return conn


def start_run(conn: sqlite3.Connection, *, src_root: Path, dst_root: Path,
              gap_hours: float, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs(started_at, src_root, dst_root, exiftool_version,"
        " schema_version, gap_hours) VALUES (?,?,?,?,?,?)",
        (started_at, str(src_root), str(dst_root), get_exiftool_version(),
         SCHEMA_VERSION, gap_hours))
    conn.commit()
    return cur.lastrowid


def record_file(conn: sqlite3.Connection, run_id: int, item: PlanItem,
                result: str, sha256: Optional[str], size: int) -> None:
    try:
        mtime = item.src.stat().st_mtime
    except OSError:
        mtime = None
    conn.execute(
        "INSERT INTO files(run_id, src_path, dst_path, event_key, capture_time,"
        " sha256, size, mtime, result) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, str(item.src), str(item.dst), item.event_key,
         item.capture_time.isoformat() if item.capture_time else None,
         sha256, size, mtime, result))


# ─────────────────────────────────────────────────────────────────────────────
# dry-run 요약  — 묶음 품질 판정용 출력 (C1)
# ─────────────────────────────────────────────────────────────────────────────

def print_dry_run(events: list[Event], quarantined: list[Unit],
                  junk: list[Unit], plan: list[PlanItem]) -> None:
    print(f"\n{'='*70}\nDRY-RUN 계획 — 디스크에 아무것도 쓰지 않음\n{'='*70}")
    single = [e for e in events if e.photo_count == 1]
    print(f"이벤트 {len(events)}개 · 파일 {sum(e.photo_count for e in events)}장"
          f" · 기타 {sum(len(u.files) for u in junk)}장"
          f" · 격리 {sum(len(u.files) for u in quarantined)}장")
    if single:
        print(f"⚠️  1장짜리 이벤트 {len(single)}개 — 갭 임계값 점검 필요할 수 있음")

    print(f"\n{'이벤트':<22}{'기간':>8}{'장수':>6}  날짜범위 · 샘플")
    print("-" * 70)
    for ev in events:
        dur = ev.end - ev.start
        dur_str = _human_duration(dur)
        sample = ev.units[0].primary.src.name
        rng = f"{ev.start:%Y-%m-%d %H:%M}~{ev.end:%m-%d %H:%M}"
        print(f"{ev.key:<22}{dur_str:>8}{ev.photo_count:>6}  {rng}  ({sample})")

    if junk:
        files = [f for u in junk for f in u.files]
        print(f"\n{JUNK_DIR}/ ({len(files)}장) — 스크린샷·배경화면·아트워크 등:")
        for f in files[:8]:
            print(f"   {f.src.name}")
        if len(files) > 8:
            print(f"   … 외 {len(files) - 8}장")

    if quarantined:
        print(f"\n{QUARANTINE_DIR}/ ({sum(len(u.files) for u in quarantined)}장):")
        reasons: dict[str, int] = defaultdict(int)
        for u in quarantined:
            for f in u.files:
                reasons[f.quarantine_reason or "unknown"] += 1
        for reason, n in sorted(reasons.items()):
            print(f"   {reason}: {n}장")
    print(f"\n{'='*70}\n실제 복사하려면 --copy 를 붙여 다시 실행하세요.\n")


def _human_duration(d: dt.timedelta) -> str:
    secs = int(d.total_seconds())
    if secs < 3600:
        return f"{secs // 60}분"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}일"


# ─────────────────────────────────────────────────────────────────────────────
# 오케스트레이션
# ─────────────────────────────────────────────────────────────────────────────

def plan_from_source(src: Path, gap_hours: float, keep_original: bool
                     ) -> tuple[list[Event], list[Unit], list[Unit], list[PlanItem]]:
    """순수 파이프라인 [2]~[5] 합성. (events, quarantined, junk, plan).

    분류 순서: 시각없음 → 격리 / 짝묶기 → 이름이 쓰레기패턴이면 _기타, 나머지는
    시각으로 이벤트화. 진짜 사진(EXIF 벗겨진 보정본·받은 사진 포함)은 이벤트로 간다.
    """
    media = load_media(src)
    quarantine_media = [m for m in media if m.is_quarantined]
    timed = [m for m in media if not m.is_quarantined]

    units = pair_live_photos(timed)
    junk = [u for u in units if u.primary.is_junk]
    real_units = [u for u in units if not u.primary.is_junk]

    events = split_events(real_units, gap_hours)
    quarantined = [_solo_unit(m) for m in quarantine_media]
    plan = build_plan(events, quarantined, junk, keep_original)
    return events, quarantined, junk, plan


def execute(plan: list[PlanItem], dst_root: Path, *, verify: bool,
            is_exfat: bool, conn: sqlite3.Connection, run_id: int) -> dict:
    """--copy 단계. 계획대로 복사 실행 + 맨페스트 기록."""
    counts = defaultdict(int)
    for item in plan:
        full_dst = dst_root / item.dst
        result, sha, size = copy_one(item.src, full_dst, verify=verify,
                                     is_exfat=is_exfat)
        record_file(conn, run_id, item, result, sha, size)
        counts[result] += 1
        symbol = {"copied": "✓", "skipped": "=", "failed": "✗",
                  "quarantined_4gb": "▣", "preserved": "+"}.get(result, "?")
        print(f"  {symbol} {item.src.name} → {item.dst}")
    conn.execute(
        "UPDATE runs SET copied=?, skipped=?, quarantined=?, failed=? WHERE id=?",
        (counts["copied"], counts["skipped"],
         counts["quarantined_4gb"], counts["failed"], run_id))
    conn.commit()
    return dict(counts)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="사진 이벤트 자동 정리기 (M1)")
    ap.add_argument("src", type=Path, help="입력 폴더(파일 더미). --review-similar 면 정리된 폴더.")
    ap.add_argument("dst", type=Path, nargs="?", help="목적지 루트(외장하드 등)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="계획만 출력(기본). 디스크에 안 씀.")
    mode.add_argument("--copy", action="store_true", help="실제 복사 실행")
    ap.add_argument("--gap-hours", type=float, default=DEFAULT_GAP_HOURS,
                    help=f"이벤트 경계 시간갭(기본 {DEFAULT_GAP_HOURS}h)")
    ap.add_argument("--no-verify", action="store_true",
                    help="복사 후 sha256 검증 끄기(기본 ON)")
    ap.add_argument("--keep-original-name", action="store_true",
                    help="새 이름에 원본 파일명 접미사 보존")
    ap.add_argument("--review-similar", action="store_true",
                    help="비슷한 사진을 브라우저에서 리뷰(남길 것 고르면 나머지는 휴지통으로). "
                         "src 자리에 '정리된 폴더'를 넣으세요(dst 무시).")
    ap.add_argument("--threshold", type=int, default=12,
                    help="유사 판정 해밍거리(작을수록 엄격, 기본 12)")
    args = ap.parse_args(argv)

    if args.review_similar:
        # 정리된 폴더 위에서 동작 — 무거운 의존성(Pillow)은 이때만 로드
        import review_similar
        return review_similar.run(args.src, threshold=args.threshold)

    src: Path = args.src
    dst: Path = args.dst
    if not src.is_dir():
        print(f"입력 폴더가 없습니다: {src}", file=sys.stderr)
        return 2
    if dst is None:
        print("목적지(dst)를 지정하세요.", file=sys.stderr)
        return 2

    try:
        events, quarantined, junk, plan = plan_from_source(
            src, args.gap_hours, args.keep_original_name)
    except RuntimeError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    if not plan:
        print("처리할 파일이 없습니다.", file=sys.stderr)
        return 0

    # 여유공간 사전점검(D6)
    need = total_source_bytes(plan)
    dst.mkdir(parents=True, exist_ok=True)
    avail = free_bytes(dst)
    is_exfat = detect_exfat(dst)
    print(f"소스 총량 {_human_bytes(need)} · 목적지 여유 {_human_bytes(avail)}"
          f"{' · exFAT' if is_exfat else ''}")
    if need > avail:
        print(f"오류: 목적지 여유공간 부족({_human_bytes(need)} 필요).",
              file=sys.stderr)
        return 1

    if not args.copy:  # 기본 = dry-run
        print_dry_run(events, quarantined, junk, plan)
        return 0

    # --copy
    conn = open_manifest(dst)
    started = _now_iso()
    run_id = start_run(conn, src_root=src, dst_root=dst,
                       gap_hours=args.gap_hours, started_at=started)
    print(f"\n복사 시작 (run #{run_id}, verify={'off' if args.no_verify else 'on'})")
    counts = execute(plan, dst, verify=not args.no_verify, is_exfat=is_exfat,
                     conn=conn, run_id=run_id)
    conn.close()
    print(f"\n완료: 복사 {counts.get('copied',0)} · skip {counts.get('skipped',0)}"
          f" · 격리(4GB) {counts.get('quarantined_4gb',0)}"
          f" · 실패 {counts.get('failed',0)}")
    return 1 if counts.get("failed") else 0


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
