#!/usr/bin/env python3
"""사진 정리기 — 통합 웹 앱.

브라우저 하나에서 전 과정을 한다 (터미널 명령 불필요):
  1) 폴더 고르기 (원본 더미 + 정리될 곳) — 클릭으로 탐색
  2) 미리보기 (dry-run): 이벤트 묶음·기타·격리 요약, 여유공간 점검 — 디스크 무쓰기
  3) 복사 실행: 원자적 복사 + sha256 검증 (원본 절대 안 건드림)
  4) 유사 사진 정리: 비슷한 묶음에서 남길 것 고르고 나머지는 휴지통(_삭제예정)으로

순수/IO 로직은 organize.py·review_similar.py 를 그대로 재사용한다.

    python3 app.py            (또는 '사진정리.command' 더블클릭)
"""

from __future__ import annotations

import json
import sys
import threading
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import organize as O
import review_similar as R

HOME = Path.home()


# ─────────────────────────────────────────────────────────────────────────────
# 폴더 탐색 (브라우저용)
# ─────────────────────────────────────────────────────────────────────────────

def list_dirs(p: Path) -> dict:
    """폴더 p 의 하위 폴더 목록 + 부모. 접근불가/없으면 홈으로 폴백."""
    try:
        p = p.expanduser().resolve()
        if not p.is_dir():
            p = HOME
    except Exception:
        p = HOME
    try:
        dirs = sorted([c.name for c in p.iterdir()
                       if c.is_dir() and not c.name.startswith(".")],
                      key=str.lower)
    except PermissionError:
        dirs = []
    parent = None if p == p.parent else str(p.parent)
    return {"path": str(p), "parent": parent, "dirs": dirs}


# ─────────────────────────────────────────────────────────────────────────────
# 단계별 작업 (organize.py 재사용)
# ─────────────────────────────────────────────────────────────────────────────

def make_plan(src: Path, gap_hours: float):
    """dry-run 계획 합성 → (events, quarantined, junk, plan)."""
    return O.plan_from_source(src, gap_hours, keep_original=False)


def plan_summary(src: Path, dst: Path, gap_hours: float) -> dict:
    events, quarantined, junk, plan = make_plan(src, gap_hours)
    need = O.total_source_bytes(plan)
    free = O.free_bytes(dst) if dst.exists() or dst.parent.exists() else 0
    rows = []
    for ev in events:
        rows.append({
            "key": ev.key,
            "count": ev.photo_count,
            "dur": O._human_duration(ev.end - ev.start),
            "range": f"{ev.start:%Y-%m-%d %H:%M}~{ev.end:%m-%d %H:%M}",
            "sample": ev.units[0].primary.src.name,
        })
    junk_files = [f.src.name for u in junk for f in u.files]
    q_files = [f.src.name for u in quarantined for f in u.files]
    return {
        "events": rows,
        "event_count": len(events),
        "event_photos": sum(e.photo_count for e in events),
        "junk": junk_files,
        "quarantine": q_files,
        "singletons": sum(1 for e in events if e.photo_count == 1),
        "need_bytes": need, "need_h": O._human_bytes(need),
        "free_bytes": free, "free_h": O._human_bytes(free),
        "enough": need <= free,
    }


def do_copy(src: Path, dst: Path, gap_hours: float) -> dict:
    events, quarantined, junk, plan = make_plan(src, gap_hours)
    need = O.total_source_bytes(plan)
    dst.mkdir(parents=True, exist_ok=True)
    if need > O.free_bytes(dst):
        return {"error": f"여유공간 부족 ({O._human_bytes(need)} 필요)"}
    is_exfat = O.detect_exfat(dst)
    conn = O.open_manifest(dst)
    run_id = O.start_run(conn, src_root=src, dst_root=dst,
                         gap_hours=gap_hours, started_at=O._now_iso())
    counts = O.execute(plan, dst, verify=True, is_exfat=is_exfat,
                       conn=conn, run_id=run_id)
    conn.close()
    return {
        "copied": counts.get("copied", 0),
        "skipped": counts.get("skipped", 0),
        "failed": counts.get("failed", 0),
        "quarantined_4gb": counts.get("quarantined_4gb", 0),
        "dst": str(dst),
    }


def similar_groups(root: Path, threshold: int) -> dict:
    R._ensure_pillow()
    groups = R.build_groups(root, threshold)
    rels = [[str(p.resolve().relative_to(root.resolve())) for p in g]
            for g in groups]
    return {"root": str(root.resolve()), "groups": rels,
            "total": sum(len(g) for g in rels)}


# ─────────────────────────────────────────────────────────────────────────────
# 웹 서버
# ─────────────────────────────────────────────────────────────────────────────

def _safe_under(root: Path, rel: str):
    rel = unicodedata.normalize("NFC", rel)
    try:
        root_r = root.resolve()
        target = (root_r / rel).resolve()
        target.relative_to(root_r)
        return target
    except (ValueError, OSError):
        return None


def serve(port: int = 8765):
    page = PAGE_HTML.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # ---- GET ----
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                return self._send(200, "text/html; charset=utf-8", page)
            if u.path == "/ls":
                start = q.get("p", [str(HOME)])[0]
                return self._json(list_dirs(Path(start)))
            if u.path == "/thumb":
                root = Path(q.get("root", [""])[0])
                target = _safe_under(root, q.get("p", [""])[0])
                if not target or not target.is_file():
                    return self._send(404, "text/plain", b"nf")
                try:
                    return self._send(200, "image/jpeg", R.make_thumbnail(target))
                except Exception:
                    return self._send(500, "text/plain", b"thumb err")
            return self._send(404, "text/plain", b"nf")

        # ---- POST ----
        def do_POST(self):
            u = urlparse(self.path)
            body = self._read_json()
            try:
                if u.path == "/plan":
                    src = Path(body["src"]); dst = Path(body.get("dst") or src)
                    if not src.is_dir():
                        return self._json({"error": "원본 폴더가 없습니다"})
                    return self._json(plan_summary(src, dst, float(body.get("gap_hours", 4))))
                if u.path == "/copy":
                    src = Path(body["src"]); dst = Path(body["dst"])
                    if not src.is_dir():
                        return self._json({"error": "원본 폴더가 없습니다"})
                    return self._json(do_copy(src, dst, float(body.get("gap_hours", 4))))
                if u.path == "/similar":
                    root = Path(body["root"])
                    if not root.is_dir():
                        return self._json({"error": "폴더가 없습니다"})
                    return self._json(similar_groups(root, int(body.get("threshold", 12))))
                if u.path == "/trash":
                    root = Path(body["root"])
                    targets = [t for r in body.get("paths", [])
                               if (t := _safe_under(root, r))]
                    return self._json({"moved": R.move_to_trash(root, targets)})
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"})
            return self._send(404, "text/plain", b"nf")

        # ---- helpers ----
        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return {}

        def _json(self, obj):
            self._send(200, "application/json", json.dumps(obj).encode("utf-8"))

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n사진 정리기 실행 중: {url}")
    print("브라우저가 자동으로 열립니다. 끝내려면 이 창에서 Ctrl+C.\n")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료.")
        httpd.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# 프런트엔드 (단일 페이지)
# ─────────────────────────────────────────────────────────────────────────────

PAGE_HTML = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>사진 정리기</title>
<style>
  :root{ color-scheme:light dark; }
  *{ box-sizing:border-box; }
  body{ font-family:-apple-system,system-ui,sans-serif; margin:0; background:#f4f4f5; color:#18181b; }
  header{ background:#fff; border-bottom:1px solid #e4e4e7; padding:16px 22px; position:sticky; top:0; z-index:20; }
  header h1{ font-size:18px; margin:0; }
  main{ max-width:1100px; margin:0 auto; padding:18px; }
  .step{ background:#fff; border:1px solid #e4e4e7; border-radius:14px; margin:14px 0; overflow:hidden; opacity:.55; }
  .step.active{ opacity:1; }
  .step.done .shead .n{ background:#16a34a; }
  .shead{ display:flex; align-items:center; gap:10px; padding:14px 18px; border-bottom:1px solid #f0f0f1; }
  .shead .n{ width:26px; height:26px; border-radius:50%; background:#a1a1aa; color:#fff; display:grid; place-items:center; font-size:14px; font-weight:600; flex:none; }
  .shead h2{ font-size:15px; margin:0; }
  .sbody{ padding:18px; }
  label.fld{ display:block; font-size:13px; color:#52525b; margin:0 0 4px; }
  .pathrow{ display:flex; gap:8px; margin-bottom:14px; }
  .pathrow input{ flex:1; padding:9px 11px; border:1px solid #d4d4d8; border-radius:8px; font-size:13px; font-family:ui-monospace,monospace; }
  button{ background:#18181b; color:#fff; border:0; border-radius:8px; padding:9px 15px; font-size:14px; cursor:pointer; }
  button.ghost{ background:#e4e4e7; color:#18181b; }
  button.go{ background:#2563eb; }
  button.danger{ background:#ef4444; }
  button:disabled{ background:#a1a1aa; cursor:default; }
  .muted{ color:#71717a; font-size:13px; }
  table{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }
  th,td{ text-align:left; padding:6px 8px; border-bottom:1px solid #f0f0f1; white-space:nowrap; }
  th{ color:#71717a; font-weight:600; }
  .tablewrap{ max-height:340px; overflow:auto; border:1px solid #f0f0f1; border-radius:8px; }
  .pills{ display:flex; gap:10px; flex-wrap:wrap; margin:6px 0 12px; }
  .pill{ background:#f4f4f5; border:1px solid #e4e4e7; border-radius:999px; padding:6px 12px; font-size:13px; }
  .pill b{ font-size:15px; }
  .warn{ color:#b45309; }
  .err{ color:#dc2626; font-size:13px; }
  .ok{ color:#16a34a; }
  /* 폴더 탐색 모달 */
  .modal{ position:fixed; inset:0; background:rgba(0,0,0,.4); display:none; place-items:center; z-index:50; }
  .modal.show{ display:grid; }
  .picker{ background:#fff; width:min(560px,92vw); max-height:80vh; border-radius:14px; display:flex; flex-direction:column; overflow:hidden; }
  .picker .cur{ padding:12px 16px; border-bottom:1px solid #eee; font-family:ui-monospace,monospace; font-size:12px; word-break:break-all; }
  .picker .list{ overflow:auto; flex:1; }
  .picker .row{ padding:10px 16px; border-bottom:1px solid #f4f4f5; cursor:pointer; font-size:14px; }
  .picker .row:hover{ background:#f4f4f5; }
  .picker .foot{ padding:12px 16px; border-top:1px solid #eee; display:flex; gap:8px; justify-content:flex-end; }
  /* 유사 사진 */
  .group{ border:1px solid #e4e4e7; border-radius:12px; margin:14px 0; }
  .ghead{ display:flex; align-items:center; gap:12px; padding:10px 14px; border-bottom:1px solid #f0f0f1; font-size:14px; }
  .cards{ display:flex; flex-wrap:wrap; gap:10px; padding:14px; }
  .card{ position:relative; width:200px; cursor:pointer; border-radius:8px; overflow:hidden; border:3px solid #ef4444; background:#000; }
  .card.keep{ border-color:#22c55e; box-shadow:0 0 0 3px #22c55e66; }
  .card img{ width:100%; height:200px; object-fit:cover; display:block; }
  .card .ck{ position:absolute; top:8px; left:8px; width:22px; height:22px; }
  .card .nm{ position:absolute; bottom:0; left:0; right:0; font-size:11px; padding:4px 6px; color:#fff; background:rgba(0,0,0,.55); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .status{ font-size:13px; }
  .done .cards{ display:none; }
</style></head><body>
<header><h1>📷 사진 정리기</h1></header>
<main>

  <section class="step active" id="s1">
    <div class="shead"><div class="n">1</div><h2>폴더 고르기</h2></div>
    <div class="sbody">
      <label class="fld">원본 사진 더미 (정리할 사진들이 있는 폴더)</label>
      <div class="pathrow"><input id="src" placeholder="예: …/photo test"><button class="ghost" onclick="pick('src')">찾아보기</button></div>
      <label class="fld">정리될 곳 (외장하드 등 — 새로 만들어집니다)</label>
      <div class="pathrow"><input id="dst" placeholder="예: …/photo test_organized"><button class="ghost" onclick="pick('dst')">찾아보기</button></div>
      <label class="fld">이벤트 구분 시간 간격 (시간) — 이만큼 안 찍히면 다른 이벤트</label>
      <div class="pathrow" style="max-width:160px"><input id="gap" type="number" value="4" step="0.5" min="0.5"></div>
      <button class="go" onclick="doPlan()">미리보기 만들기 →</button>
      <span id="s1err" class="err"></span>
    </div>
  </section>

  <section class="step" id="s2">
    <div class="shead"><div class="n">2</div><h2>미리보기 — 이렇게 정리됩니다 (아직 복사 안 함)</h2></div>
    <div class="sbody">
      <div class="pills" id="pills"></div>
      <div id="spacewarn"></div>
      <div class="tablewrap"><table id="evtbl"><thead><tr><th>이벤트(폴더)</th><th>장수</th><th>기간</th><th>날짜범위</th><th>샘플</th></tr></thead><tbody></tbody></table></div>
      <details style="margin-top:10px"><summary class="muted">기타·격리 파일 목록 보기</summary><div id="extra" class="muted" style="margin-top:8px"></div></details>
      <div style="margin-top:16px"><button class="go" id="copybtn" onclick="doCopy()">실제로 복사 실행 →</button>
        <span class="muted">원본은 절대 안 건드리고, 사본마다 sha256 검증합니다.</span>
        <span id="s2err" class="err"></span></div>
    </div>
  </section>

  <section class="step" id="s3">
    <div class="shead"><div class="n">3</div><h2>복사 완료</h2></div>
    <div class="sbody">
      <div id="copyresult"></div>
      <button class="go" id="simbtn" onclick="doSimilar()" style="margin-top:14px">비슷한 사진 정리하러 가기 →</button>
    </div>
  </section>

  <section class="step" id="s4">
    <div class="shead"><div class="n">4</div><h2>비슷한 사진 정리</h2></div>
    <div class="sbody">
      <p class="muted">맘에 드는 사진을 <b>체크</b>하세요(초록=남김). 체크 안 한 것(빨강)은 삭제 후보.
        버튼을 누르면 체크 안 한 것이 <b>_삭제예정/</b> 폴더로 <b>이동</b>(영구삭제 아님). 진짜로 지우려면 나중에 그 폴더를 직접 비우세요.</p>
      <div id="simstatus" class="muted"></div>
      <div id="groups"></div>
    </div>
  </section>
</main>

<div class="modal" id="modal"><div class="picker">
  <div class="cur" id="pkcur"></div>
  <div class="list" id="pklist"></div>
  <div class="foot"><button class="ghost" onclick="closePick()">취소</button><button onclick="choosePick()">이 폴더 선택</button></div>
</div></div>

<script>
const $=s=>document.querySelector(s);
async function post(u,b){ const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return r.json(); }
function setStep(n){ for(let i=1;i<=4;i++){ $('#s'+i).classList.toggle('active', i<=n); } }
function markDone(n){ $('#s'+n).classList.add('done'); }

// ---- 폴더 탐색 모달 ----
let pkTarget=null, pkCur='';
async function pick(target){ pkTarget=target; const start=$('#'+target).value||''; await loadDir(start); $('#modal').classList.add('show'); }
function closePick(){ $('#modal').classList.remove('show'); }
function choosePick(){ $('#'+pkTarget).value=pkCur; closePick(); }
async function loadDir(p){ const d=await fetch('/ls?p='+encodeURIComponent(p)).then(r=>r.json()); pkCur=d.path; $('#pkcur').textContent=d.path;
  let h=''; if(d.parent) h+=`<div class="row" onclick="loadDir('${esc(d.parent)}')">⬆️ 상위 폴더</div>`;
  for(const name of d.dirs){ const full=d.path.replace(/\/$/,'')+'/'+name; h+=`<div class="row" onclick="loadDir('${esc(full)}')">📁 ${escHtml(name)}</div>`; }
  $('#pklist').innerHTML=h||'<div class="row muted">하위 폴더 없음</div>'; }
function esc(s){ return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'"); }
function escHtml(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

// ---- 2단계: 미리보기 ----
async function doPlan(){
  $('#s1err').textContent=''; const src=$('#src').value.trim(), dst=$('#dst').value.trim(), gap=$('#gap').value;
  if(!src){ $('#s1err').textContent='원본 폴더를 골라주세요'; return; }
  $('#s1err').textContent='분석 중…';
  const r=await post('/plan',{src,dst,gap_hours:gap});
  if(r.error){ $('#s1err').textContent=r.error; return; }
  $('#s1err').textContent='';
  $('#pills').innerHTML=`
    <div class="pill">이벤트 <b>${r.event_count}</b>개</div>
    <div class="pill">사진 <b>${r.event_photos}</b>장</div>
    <div class="pill">기타 <b>${r.junk.length}</b>장</div>
    <div class="pill">격리 <b>${r.quarantine.length}</b>장</div>
    <div class="pill">1장짜리 ${r.singletons}개</div>`;
  $('#spacewarn').innerHTML = r.enough
    ? `<div class="muted">소스 ${r.need_h} · 목적지 여유 ${r.free_h} ✓</div>`
    : `<div class="err">⚠️ 여유공간 부족: ${r.need_h} 필요 / ${r.free_h} 남음. 다른 목적지를 고르세요.</div>`;
  const tb=$('#evtbl tbody'); tb.innerHTML=r.events.map(e=>`<tr><td>${escHtml(e.key)}</td><td>${e.count}</td><td>${e.dur}</td><td>${e.range}</td><td>${escHtml(e.sample)}</td></tr>`).join('');
  $('#extra').innerHTML = `<b>기타(${r.junk.length}):</b> ${r.junk.map(escHtml).join(', ')||'없음'}<br><b>격리(${r.quarantine.length}):</b> ${r.quarantine.map(escHtml).join(', ')||'없음'}`;
  $('#copybtn').disabled = !r.enough || !dst;
  if(!dst) $('#s2err').textContent='복사하려면 "정리될 곳"도 골라주세요';
  markDone(1); setStep(2); $('#s2').scrollIntoView({behavior:'smooth'});
}

// ---- 3단계: 복사 ----
let copiedDst='';
async function doCopy(){
  $('#s2err').textContent=''; const src=$('#src').value.trim(), dst=$('#dst').value.trim(), gap=$('#gap').value;
  if(!dst){ $('#s2err').textContent='"정리될 곳"을 골라주세요'; return; }
  if(!confirm('복사를 시작할까요? (원본은 안 건드립니다)')) return;
  $('#copybtn').disabled=true; $('#s2err').textContent='복사 중… (사진이 많으면 시간이 걸립니다)';
  const r=await post('/copy',{src,dst,gap_hours:gap});
  if(r.error){ $('#s2err').textContent=r.error; $('#copybtn').disabled=false; return; }
  $('#s2err').textContent=''; copiedDst=r.dst;
  $('#copyresult').innerHTML=`<div class="ok">✓ 복사 ${r.copied}장 · 건너뜀 ${r.skipped}장 · 실패 ${r.failed}장${r.quarantined_4gb?` · 4GB격리 ${r.quarantined_4gb}장`:''}</div><div class="muted" style="margin-top:6px">위치: ${escHtml(r.dst)}</div>`;
  markDone(2); markDone(3); setStep(3); $('#s3').scrollIntoView({behavior:'smooth'});
}

// ---- 4단계: 유사 사진 ----
let simRoot='';
async function doSimilar(){
  $('#simstatus').textContent='비슷한 사진 찾는 중…'; setStep(4); $('#s4').scrollIntoView({behavior:'smooth'});
  const r=await post('/similar',{root:copiedDst||$('#dst').value.trim(),threshold:12});
  if(r.error){ $('#simstatus').innerHTML=`<span class="err">${r.error}</span>`; return; }
  simRoot=r.root;
  if(!r.groups.length){ $('#simstatus').textContent='비슷한 사진 묶음이 없어요. 끝!'; $('#groups').innerHTML=''; return; }
  $('#simstatus').textContent=`비슷한 묶음 ${r.groups.length}개 · ${r.total}장`;
  $('#groups').innerHTML=r.groups.map((g,gi)=>`
    <section class="group" data-gi="${gi}">
      <div class="ghead"><b>묶음 ${gi+1}</b> · ${g.length}장
        <button class="danger" style="margin-left:auto" onclick="trash(${gi})">체크 안 한 것 휴지통으로</button>
        <span class="status"></span></div>
      <div class="cards">${g.map(rel=>`
        <label class="card" data-rel="${escAttr(rel)}">
          <input type="checkbox" class="ck">
          <img loading="lazy" src="/thumb?root=${encodeURIComponent(simRoot)}&p=${encodeURIComponent(rel)}">
          <span class="nm">${escHtml(rel.split('/').pop())}</span>
        </label>`).join('')}</div>
    </section>`).join('');
}
function escAttr(s){ return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;'); }
document.addEventListener('change',e=>{ if(e.target.classList.contains('ck')) e.target.closest('.card').classList.toggle('keep',e.target.checked); });
async function trash(gi){
  const sec=document.querySelector(`.group[data-gi="${gi}"]`), st=sec.querySelector('.status');
  const drop=[...sec.querySelectorAll('.card')].filter(c=>!c.querySelector('.ck').checked).map(c=>c.dataset.rel);
  if(!drop.length){ st.textContent='삭제할 게 없어요 (전부 남김으로 체크됨)'; return; }
  if(!confirm(`${drop.length}장을 _삭제예정/ 로 이동할까요? (되돌릴 수 있어요)`)) return;
  sec.querySelector('.danger').disabled=true; st.textContent='이동 중…';
  const r=await post('/trash',{root:simRoot,paths:drop});
  st.textContent=`${r.moved.length}장 휴지통으로 이동됨 ✓`; sec.classList.add('done');
}
</script>
</body></html>"""


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    serve(port)
