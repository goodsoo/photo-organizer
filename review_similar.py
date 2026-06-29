#!/usr/bin/env python3
"""유사 사진 리뷰 — 비슷한 사진(버스트·연속샷)을 묶어 브라우저에서 보여주고,
남길 것만 고르면 나머지를 휴지통(`_삭제예정/`)으로 이동한다.

데이터 손실 절대 불가(1순위 제약)를 지킨다:
  - 코드는 파일을 영구삭제(`rm`)하지 않는다. **이동만** 한다.
  - 휴지통은 정리된 폴더 안의 `_삭제예정/` — 원래 상대경로를 보존해 되돌리기 쉽다.
  - 영구삭제는 사용자가 그 폴더를 직접 비우는 것으로만 일어난다(맥 휴지통 방식).
  - 원본(소스 더미)은 애초에 건드리지 않는다 — 여기는 정리된 사본 위에서만 동작.

순수로직(해시거리·묶기)과 IO(이미지 디코드·썸네일·서버)를 분리해 Pillow 없이도
묶기 로직을 단위 테스트할 수 있다.

    python3 organize.py <정리된폴더> --review-similar
    (또는 직접: python3 review_similar.py <정리된폴더>)
"""

from __future__ import annotations

import html
import io
import json
import shutil
import sys
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

TRASH_DIR = "_삭제예정"
RESERVED_DIRS = {"_삭제예정", "_기타", "_분류안됨"}
IMG_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
DEFAULT_THRESHOLD = 12   # dHash(64bit) 해밍거리 ≤ 이 값이면 "비슷함"
THUMB_PX = 320


# ─────────────────────────────────────────────────────────────────────────────
# 순수: 해밍거리 + 유사 묶기 (Pillow 불필요 → 단위테스트 가능)
# ─────────────────────────────────────────────────────────────────────────────

def hamming(a: int, b: int) -> int:
    """두 해시(정수)의 비트 차이 수."""
    return (a ^ b).bit_count()


def group_similar(items: list[tuple[Path, int]], threshold: int = DEFAULT_THRESHOLD
                  ) -> list[list[Path]]:
    """[(path, dhash)] → 비슷한 것끼리 묶은 그룹 목록(2장 이상만).

    인접 해밍거리 ≤ threshold 면 같은 그룹(union-find). 결정적: 입력 순서대로
    묶고 그룹은 멤버수 내림차순 → 경로순으로 정렬.
    """
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for i in range(n):
        for j in range(i + 1, n):
            if hamming(items[i][1], items[j][1]) <= threshold:
                union(i, j)

    buckets: dict[int, list[Path]] = {}
    for idx, (path, _) in enumerate(items):
        buckets.setdefault(find(idx), []).append(path)

    groups = [sorted(g, key=str) for g in buckets.values() if len(g) >= 2]
    groups.sort(key=lambda g: (-len(g), str(g[0])))
    return groups


# ─────────────────────────────────────────────────────────────────────────────
# IO: dHash + 썸네일 (Pillow)
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_pillow():
    try:
        from PIL import Image  # noqa
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError as e:
        raise RuntimeError(
            "이 기능은 Pillow + pillow-heif 가 필요합니다.\n"
            "  .venv/bin/pip install Pillow pillow-heif"
        ) from e


def dhash(path: Path, size: int = 8) -> Optional[int]:
    """dHash: 회색조 (size+1)x(size) 축소 → 인접 픽셀 밝기 비교로 64bit 지문.

    디코드 실패시 None(해당 파일은 묶기에서 제외).
    """
    from PIL import Image
    try:
        with Image.open(path) as im:
            im = im.convert("L").resize((size + 1, size), Image.LANCZOS)
            px = list(im.getdata())
    except Exception:
        return None
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def make_thumbnail(path: Path, px: int = THUMB_PX) -> bytes:
    """JPEG 썸네일 바이트 생성(원본 비율 유지)."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((px, px), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82)
        return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# IO: 스캔 + 그룹 빌드
# ─────────────────────────────────────────────────────────────────────────────

def iter_event_images(root: Path):
    """정리된 폴더의 이벤트 사진들을 순회(예약폴더·휴지통·맨페스트 제외)."""
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        if sub.name in RESERVED_DIRS:
            continue
        for f in sorted(sub.iterdir()):
            if f.is_file() and f.suffix.lower() in IMG_EXTS:
                yield f


def build_groups(root: Path, threshold: int) -> list[list[Path]]:
    """이벤트별로 dHash 계산 후 유사 묶기. 이벤트 경계를 넘지 않는다
    (비슷한 버스트는 같은 이벤트 안에 있으므로 충분하고 빠르다)."""
    by_event: dict[Path, list[tuple[Path, int]]] = {}
    total = 0
    for f in iter_event_images(root):
        h = dhash(f)
        if h is None:
            continue
        by_event.setdefault(f.parent, []).append((f, h))
        total += 1
        if total % 200 == 0:
            print(f"  …해시 {total}장", file=sys.stderr)

    groups: list[list[Path]] = []
    for items in by_event.values():
        groups.extend(group_similar(items, threshold))
    # 큰 묶음부터, 그 안에서 이벤트(경로)순
    groups.sort(key=lambda g: (-len(g), str(g[0])))
    return groups


# ─────────────────────────────────────────────────────────────────────────────
# IO: 휴지통 이동 (영구삭제 아님)
# ─────────────────────────────────────────────────────────────────────────────

def move_to_trash(root: Path, files: list[Path]) -> list[str]:
    """파일들을 root/_삭제예정/<원래상대경로> 로 이동. 이동된 상대경로 목록 반환.

    영구삭제가 아니라 이동이며 원래 상대경로를 보존해 되돌리기 쉽다. 같은 이름
    충돌시 접미사. root 밖이나 휴지통 안 파일은 무시(안전).
    """
    trash = root / TRASH_DIR
    moved: list[str] = []
    for f in files:
        try:
            rel = f.resolve().relative_to(root.resolve())
        except ValueError:
            continue  # root 밖 → 안전하게 무시
        if rel.parts and rel.parts[0] == TRASH_DIR:
            continue
        if not f.is_file():
            continue
        dest = trash / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
            n += 1
        shutil.move(str(f), str(dest))
        moved.append(str(rel))
    return moved


# ─────────────────────────────────────────────────────────────────────────────
# IO: 로컬 웹 서버 (표준 라이브러리만)
# ─────────────────────────────────────────────────────────────────────────────

def _page_html(groups: list[list[Path]], root: Path) -> str:
    rels = [[str(p.resolve().relative_to(root.resolve())) for p in g] for g in groups]
    total = sum(len(g) for g in groups)
    blocks = []
    for gi, group in enumerate(rels):
        cards = []
        for pi, rel in enumerate(group):
            safe = html.escape(rel)
            name = html.escape(Path(rel).name)
            cards.append(f'''
              <label class="card drop" data-rel="{safe}">
                <input type="checkbox" class="keep">
                <img loading="lazy" src="/thumb?p={safe}">
                <span class="name">{name}</span>
              </label>''')
        blocks.append(f'''
          <section class="group" data-gi="{gi}">
            <div class="ghead">
              <b>묶음 {gi + 1}</b> · {len(group)}장
              <button class="trash" onclick="trashGroup({gi})">체크 안 한 것 휴지통으로</button>
              <span class="status"></span>
            </div>
            <div class="cards">{''.join(cards)}</div>
          </section>''')

    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>유사 사진 리뷰</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background:#f4f4f5; color:#18181b; }}
  header {{ position:sticky; top:0; background:#fff; border-bottom:1px solid #e4e4e7; padding:14px 20px; z-index:10; }}
  header h1 {{ font-size:17px; margin:0 0 4px; }}
  header p {{ margin:0; font-size:13px; color:#71717a; }}
  .group {{ background:#fff; margin:16px 20px; border:1px solid #e4e4e7; border-radius:12px; overflow:hidden; }}
  .ghead {{ display:flex; align-items:center; gap:12px; padding:10px 14px; border-bottom:1px solid #f0f0f1; font-size:14px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:10px; padding:14px; }}
  .card {{ position:relative; width:200px; cursor:pointer; border-radius:8px; overflow:hidden; border:3px solid #22c55e; background:#000; }}
  .card.drop {{ border-color:#ef4444; }}                    /* 기본=체크안됨=삭제후보(빨강) */
  .card:not(.drop) {{ box-shadow:0 0 0 3px #22c55e66; }}    /* 체크됨=남김(초록 글로우) */
  .card img {{ width:100%; height:200px; object-fit:cover; display:block; }}
  .card .keep {{ position:absolute; top:8px; left:8px; width:22px; height:22px; }}
  .card .name {{ display:block; font-size:11px; padding:4px 6px; color:#fff; background:rgba(0,0,0,.55); position:absolute; bottom:0; left:0; right:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  button.trash {{ margin-left:auto; background:#ef4444; color:#fff; border:0; border-radius:7px; padding:7px 12px; font-size:13px; cursor:pointer; }}
  button.trash:disabled {{ background:#a1a1aa; cursor:default; }}
  .status {{ font-size:13px; color:#16a34a; }}
  .done .cards {{ display:none; }}
  .hint {{ font-size:12px; color:#71717a; font-weight:normal; }}
</style></head><body>
<header>
  <h1>유사 사진 리뷰 — {len(groups)}묶음 · {total}장</h1>
  <p>맘에 드는 사진을 <b>체크</b>하세요(=남김, 초록). 체크 안 한 것(빨강)은 <b>삭제 후보</b>.
     버튼을 누르면 체크 안 한 것이 모두 <b>{TRASH_DIR}/</b> 폴더로 <b>이동</b>(영구삭제 아님).
     진짜로 지우려면 나중에 그 폴더를 직접 비우세요.</p>
</header>
<main>{''.join(blocks)}</main>
<script>
async function trashGroup(gi) {{
  const sec = document.querySelector(`.group[data-gi="${{gi}}"]`);
  const btn = sec.querySelector('.trash');
  const status = sec.querySelector('.status');
  const drop = [...sec.querySelectorAll('.card')]
    .filter(c => !c.querySelector('.keep').checked)
    .map(c => c.dataset.rel);
  if (drop.length === 0) {{ status.textContent = '삭제할 게 없어요 (전부 남김으로 체크됨)'; return; }}
  if (!confirm(`${{drop.length}}장을 ${{'{TRASH_DIR}'}}/ 로 이동할까요? (되돌릴 수 있어요)`)) return;
  btn.disabled = true; status.textContent = '이동 중…';
  const res = await fetch('/trash', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{paths: drop}})}});
  const out = await res.json();
  status.textContent = `${{out.moved.length}}장 휴지통으로 이동됨 ✓`;
  sec.classList.add('done');
}}
// 썸네일 클릭으로 체크 토글 시 테두리 색
document.addEventListener('change', e => {{
  if (e.target.classList.contains('keep')) {{
    e.target.closest('.card').classList.toggle('drop', !e.target.checked);
  }}
}});
</script>
</body></html>'''


def serve(root: Path, groups: list[list[Path]], port: int) -> None:
    page = _page_html(groups, root).encode("utf-8")
    root_resolved = root.resolve()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 조용히
            pass

        def _safe_path(self, rel: str) -> Optional[Path]:
            # 경로 traversal 방지: root 안으로만 해석
            rel = unicodedata.normalize("NFC", rel)
            target = (root_resolved / rel).resolve()
            try:
                target.relative_to(root_resolved)
            except ValueError:
                return None
            return target

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif parsed.path == "/thumb":
                rel = parse_qs(parsed.query).get("p", [""])[0]
                target = self._safe_path(rel)
                if not target or not target.is_file():
                    self._send(404, "text/plain", b"not found"); return
                try:
                    self._send(200, "image/jpeg", make_thumbnail(target))
                except Exception:
                    self._send(500, "text/plain", b"thumb error")
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if urlparse(self.path).path != "/trash":
                self._send(404, "text/plain", b"not found"); return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                rels = body.get("paths", [])
            except json.JSONDecodeError:
                self._send(400, "text/plain", b"bad json"); return
            targets = []
            for rel in rels:
                t = self._safe_path(rel)
                if t:
                    targets.append(t)
            moved = move_to_trash(root, targets)
            self._send(200, "application/json",
                       json.dumps({"moved": moved}).encode("utf-8"))

        def _send(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n유사 사진 리뷰 서버: {url}")
    print("브라우저에서 남길 사진을 고르고 버튼을 누르세요. 끝나면 이 터미널에서 Ctrl+C.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료.")
        httpd.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────────────────────

def run(root: Path, threshold: int = DEFAULT_THRESHOLD, port: int = 8765) -> int:
    if not root.is_dir():
        print(f"폴더가 없습니다: {root}", file=sys.stderr)
        return 2
    _ensure_pillow()
    print(f"유사 사진 스캔 중… (해밍 임계값 {threshold})", file=sys.stderr)
    groups = build_groups(root, threshold)
    if not groups:
        print("비슷한 사진 묶음을 찾지 못했어요. (임계값을 올려보려면 --threshold)")
        return 0
    n = sum(len(g) for g in groups)
    print(f"비슷한 묶음 {len(groups)}개 · 총 {n}장 발견.", file=sys.stderr)
    serve(root, groups, port)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="유사 사진 리뷰 (휴지통 방식)")
    ap.add_argument("root", type=Path, help="정리된 폴더 (organize.py 결과)")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help=f"유사 판정 해밍거리(작을수록 엄격, 기본 {DEFAULT_THRESHOLD})")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    return run(args.root, args.threshold, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
