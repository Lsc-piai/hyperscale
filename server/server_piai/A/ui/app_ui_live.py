"""
app_ui_live.py  (A/ui/ — 서버 A 안에 포함)
- 서버 A가 처리 중인 통화의 파이프라인 진행 상황을 "실시간 단계별"로 보여주는 표시 전용 UI.
- client_uploader → 서버 A(/upload_audio) 로 음성을 보내면, A가 처리하면서 단계별 중간결과를
  공유 파일(live_progress.json)에 기록한다. 이 UI는 그 파일을 1초마다 읽어 화면에 그린다.
  (즉 이 UI 자체는 파이프라인을 돌리지 않는다 — 업로드 입력이 없다.)
- 9000 대시보드(결과)와 함께 두 화면을 같이 띄워, 진행상황(7860) + 결과(9000)를 동시에 본다.
- 실행: (server 루트에서) python A/ui/app_ui_live.py   (기본 http://127.0.0.1:7860)
"""
import base64
import html as _html
import importlib.util
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# A/ 를 sys.path에 추가 → `app.*` import 가능.
A_ROOT = Path(__file__).resolve().parents[1]
if str(A_ROOT) not in sys.path:
    sys.path.insert(0, str(A_ROOT))

import gradio as gr


def _load_live_progress():
    """`live_progress` 모듈을 가져온다 (패키지 → 실패하면 파일 경로).

    서버 A 머신에서는 `app.core.live_progress` 로 그냥 import 된다. 그런데 그 한 줄이
    `app/__init__.py` 를 실행하고, 거기서 `lifespan` → `core.config`(torch/transformers) ·
    `core.search`(faiss) · `core.llm` 을 끌어온다. **워크스테이션에는 그 의존성이 없으므로
    패키지 import 는 반드시 실패한다** — UI 는 gradio 만 있으면 되는데도.

    그래서 실패하면 `live_progress.py` 를 파일로 직접 로드한다. 워크스테이션 설치는
    "이 파일과 live_progress.py 를 같은 폴더에 두기"로 끝난다. 상수(STEPS/KW_LABELS)를
    여기에 복사해두지 않는 이유는 원본이 바뀌면 조용히 어긋나기 때문이다.
    """
    try:
        from app.core import live_progress as m
        return m
    except Exception as e:
        first = str(e).split("\n")[0]

    cands = []
    if os.environ.get("LIVE_PROGRESS_MODULE"):
        cands.append(Path(os.environ["LIVE_PROGRESS_MODULE"]))
    here = Path(__file__).resolve().parent
    cands += [here / "live_progress.py", A_ROOT / "app" / "core" / "live_progress.py"]
    for c in cands:
        if c.is_file():
            spec = importlib.util.spec_from_file_location("live_progress_standalone", c)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            print(f"[app_ui_live] live_progress 를 파일로 로드: {c}", flush=True)
            return m
    raise SystemExit(
        f"[app_ui_live] live_progress 모듈을 못 찾았다.\n"
        f"  패키지 import 실패: {first}\n"
        f"  찾아본 경로: " + " / ".join(str(c) for c in cands) + "\n"
        f"  워크스테이션에서 돌린다면 live_progress.py 를 이 스크립트와 같은 폴더에 두거나 "
        f"LIVE_PROGRESS_MODULE 로 경로를 줄 것.")


_lp = _load_live_progress()
read_progress_file = _lp.read_progress
STEPS, KW_LABELS = _lp.STEPS, _lp.KW_LABELS

# ── 진행상황을 어디서 읽을지 ────────────────────────────────────
# 기본: 서버 A 와 같은 머신에서 돌면서 live_progress.json 파일을 직접 읽는다(기존 동작).
# LIVE_PROGRESS_URL 을 주면 HTTP 로 읽는다 → **UI 를 워크스테이션에서 돌릴 수 있다.**
#
#   워크스테이션:  ssh -N -L 8000:localhost:8000 n1
#                  LIVE_PROGRESS_URL=http://localhost:8000/live_progress python app_ui_live.py
#
# 이러면 서버 쪽에 포트를 열 필요가 없다 — 터널 안에서만 오간다. A 의 엔드포인트도
# 루프백에서만 응답하므로(`app/api/routes_live_progress.py`) 터널 외 경로는 막혀 있다.
#
# 실패를 조용히 넘기지 않는다: 연결이 끊기면 마지막 값을 계속 그리는 게 아니라
# None 을 돌려 '대기 중' 화면이 되게 하고, 오류 내용을 최초 1회만 찍는다.
# (0.1초마다 폴링하므로 매번 찍으면 로그가 못 쓰게 된다.)
LIVE_PROGRESS_URL = os.environ.get("LIVE_PROGRESS_URL", "")
# 서버 A 가 LIVE_PROGRESS_TOKEN 을 설정했으면 같은 값을 줘야 한다.
LIVE_PROGRESS_TOKEN = os.environ.get("LIVE_PROGRESS_TOKEN", "")
_FETCH_TIMEOUT = float(os.environ.get("LIVE_PROGRESS_TIMEOUT", "2.0"))
_fetch_err = {"last": None}


def read_progress():
    if not LIVE_PROGRESS_URL:
        return read_progress_file()
    try:
        req = urllib.request.Request(LIVE_PROGRESS_URL)
        if LIVE_PROGRESS_TOKEN:
            req.add_header("X-Live-Token", LIVE_PROGRESS_TOKEN)
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        if _fetch_err["last"] is not None:
            print(f"[app_ui_live] 진행상황 수신 복구: {LIVE_PROGRESS_URL}", flush=True)
            _fetch_err["last"] = None
        return data if isinstance(data, dict) else None
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if msg != _fetch_err["last"]:
            _fetch_err["last"] = msg
            hint = ""
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                hint = ("  → 404 다. ①A 가 구버전(라우트 없음) ②출처 IP 가 허용목록 밖"
                        "(LIVE_PROGRESS_ALLOW) ③X-Live-Token 불일치 중 하나다."
                        f" 토큰 전송={'ON' if LIVE_PROGRESS_TOKEN else 'OFF'}."
                        " A 로그의 '[live_progress] 거절' 줄이 어느 쪽인지 알려준다.")
            print(f"[app_ui_live][WARN] {LIVE_PROGRESS_URL} 읽기 실패 — {msg}{hint}", flush=True)
        return None


# ── 디자인 ──────────────────────────────────────────────────────
# 색은 전부 CSS 변수(--lp-*)로 한 곳에 모았다. 상태(대기/진행/완료/오류)마다
# 배경·글자·점 세 가지를 한 벌로 정의하고, 흐름 칩·섹션 칩·상태 배지가 같은 벌을
# 나눠 쓴다 — 한 상태의 색을 바꾸면 화면 전체가 같이 바뀐다.
# 다크모드는 `.dark` 에서 같은 변수만 다시 정의한다(선택자를 두 배로 쓰지 않는다).
CSS = """
:root {
    --lp-surface:   #ffffff;
    --lp-inset:     #f7f9fc;
    --lp-border:    #e4e9f0;
    --lp-text:      #17212f;
    --lp-dim:       #6c7a90;
    --lp-idle-bg:   #eef1f6;  --lp-idle-fg: #97a3b6;  --lp-idle-dot: #d8dee8;
    --lp-run-bg:    #eaeeff;  --lp-run-fg:  #3a49c0;  --lp-run-dot:  #4f5fd8;  --lp-run-bd: #bcc5f7;
    --lp-done-bg:   #e6f6ed;  --lp-done-fg: #10774a;  --lp-done-dot: #17a25e;
    --lp-err-bg:    #fdeceb;  --lp-err-fg:  #c02f26;  --lp-err-dot:  #e1503f;
    /* 말풍선. 진행중(run) 색과 일부러 다른 벌을 쓴다 — 같은 색이면 '진행 중 표시'로 읽힌다 */
    --lp-bub-you:   #ffffff;  --lp-bub-you-bd: #e0e6ef;
    --lp-bub-me:    #eceff8;  --lp-bub-me-bd:  #d3daea;
    --lp-shadow:    0 1px 2px rgba(16,24,40,.04), 0 8px 20px rgba(16,24,40,.05);
    --lp-radius:    16px;
    /* 키워드 아이콘 색. ★ 0(신고자 위치)·4(냄새 위치)는 **지도 마커와 같은 색**이다
       (.mpin-rep #2b49ad / .mpin-odor #d03b3b). 같은 것을 가리키는데 색이 다르면
       사용자가 다른 항목으로 읽는다 — 지도와 키워드가 한 화면에 같이 보이므로 특히. */
    --kw-c0-fg: #2b49ad;  --kw-c0-bg: #e8ecfb;  --kw-c0-bd: #c6d0f2;
    --kw-c1-fg: #0d8f84;  --kw-c1-bg: #e2f5f2;  --kw-c1-bd: #b9e3dd;
    --kw-c2-fg: #b8690a;  --kw-c2-bg: #fdf0dd;  --kw-c2-bd: #f0d5a8;
    --kw-c3-fg: #6f57c9;  --kw-c3-bg: #eeeafb;  --kw-c3-bd: #d3cbf1;
    --kw-c4-fg: #d03b3b;  --kw-c4-bg: #fceceb;  --kw-c4-bd: #f3cac6;
}
.dark {
    --lp-surface:   #171b22;
    --lp-inset:     #1e222b;
    --lp-border:    #2b313c;
    --lp-text:      #e5eaf1;
    --lp-dim:       #8a95a7;
    --lp-idle-bg:   #232831;  --lp-idle-fg: #6b7688;  --lp-idle-dot: #333a46;
    --lp-run-bg:    #1c2340;  --lp-run-fg:  #9aaaf7;  --lp-run-dot:  #5b6ce0;  --lp-run-bd: #38457c;
    --lp-done-bg:   #12301f;  --lp-done-fg: #55c98a;  --lp-done-dot: #1fa15e;
    --lp-err-bg:    #391714;  --lp-err-fg:  #ef9184;  --lp-err-dot:  #d84a39;
    --lp-bub-you:   #232833;  --lp-bub-you-bd: #333a47;
    --lp-bub-me:    #2a3243;  --lp-bub-me-bd:  #3b465c;
    --lp-shadow:    0 1px 2px rgba(0,0,0,.3), 0 10px 26px rgba(0,0,0,.3);
    /* 어두운 배경에선 같은 색을 그대로 쓰면 대비가 무너진다 → 밝은 톤으로 바꾸고
       배지 바탕은 낮은 채도로 (dark-mode-pairing: 반전이 아니라 별도 벌을 잡는다) */
    --kw-c0-fg: #8ea6f5;  --kw-c0-bg: #1d2440;  --kw-c0-bd: #33406e;
    --kw-c1-fg: #4fc9bd;  --kw-c1-bg: #12302d;  --kw-c1-bd: #24534d;
    --kw-c2-fg: #e0a44b;  --kw-c2-bg: #33260f;  --kw-c2-bd: #5b431b;
    --kw-c3-fg: #a795ee;  --kw-c3-bg: #241f3d;  --kw-c3-bd: #3f3668;
    --kw-c4-fg: #f1948a;  --kw-c4-bg: #391714;  --kw-c4-bd: #632b25;
}

/* 폭은 화면을 그대로 쓴다 (Soft 테마가 컨테이너에 max-width 를 걸어 가운데 좁은 단으로
   만들어 버리므로 푼다). */
.gradio-container {
    max-width: 100% !important; width: 100% !important; padding: 14px 22px 16px !important;
    /* Gradio 는 여기에 flex-grow:1 · min-height 를 인라인으로 걸어 컨테이너를 화면 높이만큼
       늘린다. 그러면 offsetHeight 가 '내용 높이'가 아니라 항상 화면 높이가 되어, FIT_JS 가
       텍스트창을 줄여도 값이 안 변한다 → 매 틱 조금씩 계속 줄어드는 폭주가 된다.
       내용 높이를 그대로 반영하게 늘어남을 끈다. */
    flex-grow: 0 !important; min-height: 0 !important; height: auto !important;
}
/* 최상위 블록들의 실제 부모는 .contain 이 아니라 그 안의 .column 이다 (fill_width 때문에
   Gradio 가 한 겹 더 감싼다). 여기에 걸어야 카드 사이 간격이 실제로 줄어든다 — 실측 전에는
   .contain 에 걸어놨고, 정작 쓰이는 gap 은 16px 이었다. */
.gradio-container .contain, .gradio-container .contain > .column { gap: 11px !important; }
/* .main.app 의 상하 패딩 32px 은 순수 낭비다 (좌우는 컨테이너가 이미 준다) */
.gradio-container > .main { padding-top: 0 !important; padding-bottom: 0 !important; }
/* Gradio 기본 푸터("Use via API · Built with Gradio · Settings") 37px 회수.
   표시 전용 화면이라 쓸 일이 없고, 그만큼 대화가 더 보인다. */
footer { display: none !important; }

/* ── 세로: 아무것도 가두지 않는다 ──────────────────────────────────
   Gradio 는 컨테이너·.main.app 에 height:100% 를 건다. 내용이 그보다 길면 어느
   조상에서 잘리거나 스크롤이 막혀서 "아래가 안 보이고 내려가지도 않는" 상태가 된다.
   그래서 세로 제약을 전부 푼다 — 내용이 길면 페이지가 그냥 스크롤되게 한다.
   (한 화면에 딱 맞추는 건 잘림·갇힘보다 우선순위가 낮다) */
/* 화면 고정: 페이지 스크롤 없음. FIT_JS 가 다 들어오게 맞춰주므로 잘릴 게 없다.
   단 화면이 아주 짧으면(텍스트창 최소 160px 로도 안 들어감) 스크롤을 살린다 —
   그 경우 hidden 이면 아래가 잘려서 아예 못 본다. 1366x768 은 들어가고 1024x600 은
   안 들어가는 것을 실측해서 700px 을 경계로 잡았다. */
html, body { height: auto !important; overflow: hidden !important; }
@media (max-height: 700px) {
    html, body { overflow-y: auto !important; }
}
.gradio-container, .gradio-container > .main, .gradio-container .wrap {
    height: auto !important; max-height: none !important; min-height: 0 !important;
    overflow: visible !important;
}

/* ── 머리말 ─────────────────────────────────────────── */
.head-card { gap: 11px !important; }
.app-head { text-align: center; padding: 0; }
.app-title {
    margin: 0; font-size: 25px; font-weight: 800; letter-spacing: -.02em;
    color: var(--lp-text);
}
.app-sub { margin-top: 5px; font-size: 14.5px; color: var(--lp-dim); }

/* ── 카드(머리말 / 3분할 창 / 키워드) ────────────────── */
.head-card, .pane {
    background: var(--lp-surface) !important;
    border: 1px solid var(--lp-border) !important;
    border-radius: var(--lp-radius) !important;
    box-shadow: var(--lp-shadow);
    padding: 13px 16px !important;
}
.pane { gap: 10px !important; min-height: 0 !important; }

/* ── 제목 아래 줄: 오류가 있을 때만 나온다 ─────────────────
   평소엔 progress_html 이 빈 값을 주므로 이 칸 자체를 접어 둔다 (제목 밑에 빈 틈이 남지
   않게). Gradio 래퍼 안에 자식 요소가 늘 있어 :empty 로는 못 잡으므로 :has 로 뒤집는다. */
.topbar-holder { display: none !important; }
.topbar-holder:has(.rail-err) {
    display: block !important;
    padding: 0 !important; border: 0 !important; background: transparent !important;
}
.rail-err {
    margin-top: 9px; padding: 8px 12px; border-radius: 10px;
    background: var(--lp-err-bg); color: var(--lp-err-fg); font-size: 15px; font-weight: 600;
}
/* 깜박임은 transform·opacity 로만 만든다. box-shadow 애니메이션은 합성이 안 돼서
   매 프레임 페인트가 돌고, 그 비용이 스크롤과 같은 메인 스레드에서 나간다. */
@keyframes ring {
    0%   { transform: scale(1);    opacity: .40; }
    70%  { transform: scale(1.06); opacity: 0; }
    100% { transform: scale(1.06); opacity: 0; }
}
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }

/* ── 섹션 칩: 위 흐름 칩과 같은 상태 색을 쓴다 ────────── */
.chip-line { margin: 0 !important; padding: 0 !important; border: 0 !important; }
.section-chip {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 6px 14px; border-radius: 999px; font-size: 16.5px; font-weight: 700;
    background: var(--lp-idle-bg); color: var(--lp-idle-fg);
    transition: background .3s, color .3s;
}
.section-chip::before {
    content: ""; width: 7px; height: 7px; border-radius: 50%;
    background: currentColor; opacity: .75;
}
/* 칸마다 고유색 (data-sec). 네 칩이 다 같은 초록으로 끝나던 걸 칸별로 갈랐다.
   상태는 그대로 읽힌다: 대기=회색(아직 안 지나감) / 진행·완료=그 칸 색 / 오류=빨강(공통).
   진행중과 완료는 앞의 점이 깜박이는지로 구분되고, 카드 전체 글로우가 한 번 더 알려준다.
   색 순서는 위 진행 바(남보라 → 초록 그라데이션)를 따라간다: 파랑 → 보라 → 청록 → 초록.
   넷 다 변경 표시(빨강)와 안 겹치는 계열이다. */
.section-chip { --sc-bg: var(--lp-done-bg); --sc-fg: var(--lp-done-fg); }
.section-chip[data-sec="0"] { --sc-bg: #e4ebfc; --sc-fg: #2b49ad; }   /* STT 결과 — 파랑 */
.section-chip[data-sec="1"] { --sc-bg: #f1e9fc; --sc-fg: #6a3da6; }   /* 사투리 보정 — 보라 */
.section-chip[data-sec="2"] { --sc-bg: #ddf0f3; --sc-fg: #0d6b76; }   /* 표준어 변환 — 청록 */
.section-chip[data-sec="3"] { --sc-bg: #e2f4e9; --sc-fg: #0f7347; }   /* 키워드 추출 — 초록 */
.dark .section-chip[data-sec="0"] { --sc-bg: #1a2450; --sc-fg: #8ba6f5; }
.dark .section-chip[data-sec="1"] { --sc-bg: #2a1f42; --sc-fg: #b795ef; }
.dark .section-chip[data-sec="2"] { --sc-bg: #10333a; --sc-fg: #63c6d4; }
.dark .section-chip[data-sec="3"] { --sc-bg: #12301f; --sc-fg: #55c98a; }
.section-chip.running,
.section-chip.done  { background: var(--sc-bg); color: var(--sc-fg); }
/* 진행 중에는 칩 앞의 점만 깜박인다 (칩 전체가 아니라 — 카드 글로우와 겹쳐 시끄러워진다) */
.section-chip.running::before { animation: blink 1s ease-in-out infinite; }
.section-chip.error { background: var(--lp-err-bg);  color: var(--lp-err-fg); }

/* ── 진행 표시를 '화면에 보이는 진행'에 맞춘다 ────────────────────────
   서버 단계는 타이핑보다 앞서간다: 결과가 도착하면 그 칸 칩은 곧 '완료'가 되고 다음 칸이
   '진행 중'으로 넘어가지만, 화면에서는 아직 그 칸 대화가 흘러나오는 중이다. 그러면 다 나오지도
   않은 칸의 반짝임이 먼저 꺼지고 다음 칸이 반짝여 앞뒤가 안 맞는다.
   → JS(syncPanes)가 타이핑 상태를 보고 두 표식을 붙인다. 서버가 준 칩 클래스는 건드리지
     않는다(폴링이 되돌려 놓으므로) — 바깥 .pane 의 클래스로 눌러쓴다.
     .pane-typing: 타이핑이 남았다 → 완료로 보이지 말고 계속 진행 중으로
     .pane-hold  : 내 차례가 아직 안 왔다(앞 칸이 타이핑 중) → 진행 중을 대기로 되돌린다 */
.pane.pane-typing .section-chip { background: var(--sc-bg); color: var(--sc-fg); }
.pane.pane-typing .section-chip::before { animation: blink 1s ease-in-out infinite; }
.pane.pane-hold .section-chip:not(.error) {   /* 오류 칩(빨강)은 절대 대기로 덮지 않는다 */
    background: var(--lp-idle-bg) !important; color: var(--lp-idle-fg) !important;
}
.pane.pane-hold .section-chip:not(.error)::before { animation: none; }

/* ── 실행 중인 단계의 '바깥 카드' 전체가 반짝인다 (칩 대신) ──────────
   box-shadow 로 퍼지는 링. .pane 의 box-shadow 는 !important 가 아니므로 애니메이션이
   이긴다(important 였다면 애니메이션이 못 이긴다). 활성 여부는 JS(syncPanes)가 칩의
   running 상태를 읽어 .pane-running 을 붙였다 뗀다. */
/* 링을 더 크고 진하게 (spread 16px·불투명도 .58) + 안쪽 테두리도 같이 맥박쳐 카드
   전체가 확실히 '살아 있는' 느낌을 준다. */
@keyframes paneGlow {
    0%   { box-shadow: var(--lp-shadow), 0 0 0 0 rgba(79,95,216,.58), inset 0 0 0 2px rgba(79,95,216,.55); }
    70%  { box-shadow: var(--lp-shadow), 0 0 0 16px rgba(79,95,216,0), inset 0 0 0 2px rgba(79,95,216,.12); }
    100% { box-shadow: var(--lp-shadow), 0 0 0 0 rgba(79,95,216,0), inset 0 0 0 2px rgba(79,95,216,.55); }
}
.pane.pane-running { animation: paneGlow 1.4s ease-in-out infinite; }
.dark .pane.pane-running { animation-name: paneGlowDark; }
@keyframes paneGlowDark {
    0%   { box-shadow: var(--lp-shadow), 0 0 0 0 rgba(122,167,240,.62), inset 0 0 0 2px rgba(122,167,240,.6); }
    70%  { box-shadow: var(--lp-shadow), 0 0 0 16px rgba(122,167,240,0), inset 0 0 0 2px rgba(122,167,240,.14); }
    100% { box-shadow: var(--lp-shadow), 0 0 0 0 rgba(122,167,240,0), inset 0 0 0 2px rgba(122,167,240,.6); }
}

/* ── 텍스트 창 (A는 내용 HTML만 주고, 박스 모양은 여기서 제어) ──
   스크롤은 바깥 .diffbox 하나만. 안쪽 요소는 고정 높이를 풀어(auto) 내용대로 늘어나고,
   바깥 프레임이 그걸 잘라 스크롤 → 스크롤바 1개 + 텍스트가 박스 밖으로 안 나감 */
/* 높이를 반드시 고정한다 — !important 가 없으면 Gradio 규칙이 이겨서 박스가 내용만큼
   늘어나고, 그만큼 아래 키워드 카드가 화면 밖으로 밀린다. 높이가 묶여 있어야 넘친
   대화가 이 박스 안에서만 스크롤된다(스크롤바 1개).
   여기 값은 JS 가 돌기 전(또는 못 도는 경우)의 초기값이다. 430px 은 텍스트창을 뺀
   나머지(제목카드·섹션칩·키워드카드·패딩·간격)의 대략적 합이고, FIT_JS 가 실측으로
   덮어쓴다. 어긋나도 최악은 '페이지가 조금 스크롤된다' 뿐 — 잘리지 않는다. */
.diffbox {
    height: calc(100vh - 430px) !important; min-height: 160px !important;
    /* flex:0 0 auto 라야 위 height 가 진짜 최종값이 된다. Gradio 블록 기본값(flex-grow:1)
       이면 FIT_JS 가 높이를 줄이는 즉시 flex 가 남는 만큼 도로 늘려버려서, 계산은 맞는데
       화면은 안 줄어드는 무한 줄다리기가 된다 (실측: 페이지가 30px 넘쳐 잘려 있었다). */
    flex: 0 0 auto !important;
    /* overscroll-behavior: 박스 끝에 닿았을 때 페이지로 스크롤이 넘어가지 않게 한다.
       (페이지가 잠겨 있어 넘어갈 곳도 없는데, 브라우저가 넘길지 판단하는 동안 한 박자
       걸려서 '걸리는' 느낌이 난다) */
    overflow: auto !important; overscroll-behavior: contain; box-sizing: border-box;
    background: var(--lp-inset) !important;
    border: 1px solid var(--lp-border) !important; border-radius: 12px !important;
    /* 아래 패딩은 0 이다 — 스크롤 컨테이너의 padding-bottom 은 넘친 flex 자식의
       스크롤 범위에 포함되지 않아, 바닥까지 내려도 마지막 말풍선이 끝에 붙어 잘려 보인다.
       그래서 아래 여백은 안쪽 .chat 이 갖는다(내용의 패딩은 스크롤 범위에 포함된다). */
    padding: 14px 15px 0 !important;
    color: var(--lp-text); font-size: 17px; line-height: 1.7;
    white-space: pre-wrap; word-break: break-word;
    /* block 으로 못박는다. Gradio 의 .block 은 flex 컨테이너인데, 그러면 안쪽 래퍼가
       flex 아이템이 되어 내용만큼 커지지 않고(아래 리셋의 min-height:0 과 맞물려)
       대화가 래퍼 밖으로 삐져나간다. 삐져나간 부분은 스크롤 범위에서 빠지므로
       **바닥까지 내려도 마지막 말풍선에 닿지 못한다**(실측: scrollHeight 685 vs 내용 966). */
    display: block !important;
}
/* 안쪽 래퍼는 내용만큼 커지고 줄어들지 않는다.
   :not([data-lp]) 가 필수다 — .diffbox 는 안쪽 .prose 에도 붙으므로, 이 규칙이 그 자식인
   .chat 까지 잡아 display:block 으로 만들면 flex 정렬이 죽어 민원인 말풍선이 오른쪽으로
   안 붙는다. */
.diffbox > *:not([data-lp]) { flex: 0 0 auto !important; display: block !important; }

/* Gradio 는 elem_classes 를 블록(.block)과 내부 내용(.prose) **양쪽에** 붙인다.
   그래서 위 .diffbox 규칙이 둘 다에 걸려 '613px 스크롤러 안의 613px 스크롤러'가 됐고,
   바깥은 74px 밖에 안 밀려서 마지막 말풍선에 닿지 못했다(실측).
   중첩된 안쪽은 높이·스크롤을 놓고 내용만큼 자라게 한다 → 스크롤러는 바깥 하나. */
.diffbox .diffbox {
    height: auto !important; min-height: 0 !important; max-height: none !important;
    overflow: visible !important; padding: 0 !important; border: 0 !important;
    border-radius: 0 !important; background: transparent !important; box-shadow: none !important;
}
/* Gradio 가 .diffbox 안에 끼우는 래퍼들의 테두리·여백·고정높이를 없앤다.
   우리가 만든 요소는 data-lp 를 달고 오므로 제외한다 — 아니면 여기서 다 납작해진다.
   (클래스마다 :not() 을 붙이면 마크업을 늘릴 때마다 여기도 고쳐야 해서 조용히 깨진다)
   말풍선 '안'의 diff span 은 서버가 만들어 data-lp 가 없지만 초기화돼도 무해하다
   — 색·굵기만 인라인으로 갖는다. */
/* [data-diff] (변경 태그 span)은 리셋에서 제외한다 — 안 그러면 Gradio 가 이 리셋에
   스코프를 붙여 특이도를 높여서 아래 배지 규칙의 background 를 눌러버린다(투명 유지). */
.diffbox *:not([data-lp]):not([data-diff]) {
    border: 0 !important; background: transparent !important; box-shadow: none !important;
    padding: 0 !important; margin: 0 !important;
    height: auto !important; min-height: 0 !important; max-height: none !important;
    max-width: 100% !important; white-space: pre-wrap; word-break: break-word;
}

/* ── STT 실시간 타이핑 (step 0, 청크 누적) ───────────────────────────
   LLM 답변처럼 글자가 흘러나오는 표시. 실제 노출은 JS(typeStt)가 하고, 여기선 글꼴·커서만. */
.stt-stream {
    color: var(--lp-text); font-size: 17px; line-height: 1.75;
    white-space: pre-wrap; word-break: break-word; padding: 2px 1px 14px;
}
.stt-caret {
    display: inline-block; width: 2px; height: 1.05em; margin-left: 2px;
    vertical-align: text-bottom; background: var(--lp-run-dot);
    animation: sttblink 1s step-end infinite;
}
@keyframes sttblink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

/* ── STT 전사 완료 후 '민원 분석중…' 로딩 화면 (화자분리 전) ────────────
   도는 링(스피너)은 몇 번을 손봐도 "안 보인다"는 피드백이 계속 나왔다 → 링을 버리고
   **큰 글씨 + 점 세 개가 커졌다 작아지는** 표시로 바꿨다. 폰트와 transform 만 쓰므로
   애니메이션이 안 돌아도 글자는 무조건 보인다.
   어느 단(전사 타이핑 / 로딩 / 말풍선)을 보여줄지는 JS(typeStt)가 정한다 — 마지막 청크
   타이핑이 다 끝난 뒤에만 로딩으로 넘어간다(예전엔 파이썬이 바로 갈아끼워 글자가 잘렸다). */
/* 로딩 글자를 칸 가운데에 두려면 이 통이 칸 높이를 다 차지해야 한다. %는 부모의 content
   box 기준이라 .diffbox 패딩을 안 넘어서 스크롤바가 생기지 않는다. */
.stt-stage { display: flex; flex-direction: column; min-height: 100%; box-sizing: border-box; }
.stt-loading {
    display: none;                 /* 순서가 되면 JS 가 인라인 display:flex 로 켠다 */
    flex-direction: column; align-items: center; justify-content: center;
    gap: 10px; flex: 1 1 auto; min-height: 200px; color: var(--lp-text);
}
.stt-loading-txt {
    display: flex; align-items: baseline;
    font-size: 30px; font-weight: 800; letter-spacing: .01em;
}
.stt-dots { display: inline-flex; align-items: baseline; margin-left: 1px; }
.stt-dots i {
    display: inline-block; font-style: normal; transform-origin: 50% 80%;
    animation: sttdot 1.05s ease-in-out infinite;
}
.stt-dots i:nth-child(2) { animation-delay: .17s; }
.stt-dots i:nth-child(3) { animation-delay: .34s; }
@keyframes sttdot {
    0%, 70%, 100% { transform: scale(.45); opacity: .3; }
    35%           { transform: scale(1.45); opacity: 1; }
}

/* ── 대화 말풍선 ─────────────────────────────────────── */
.chat { display: flex; flex-direction: column; gap: 10px; padding-bottom: 14px; }
/* 처리가 끝난 대화는 챗봇처럼 **타이핑**으로 나온다 — 말풍선이 통째로 툭 뜨고 1.5초 쉬는
   방식이 아니라, STT 전사 타이핑과 **같은 속도**로 글자가 흘러나오고 한 말풍선이 끝나면
   쉼 없이 다음 말풍선으로 이어진다. 실제 진행은 JS(typeChats)가 한다.

   숨기는 일은 **CSS 가** 맡는다. JS 로 숨기면 폴링이 HTML 을 꽂은 시점과 JS 가 도는
   시점 사이에 대화가 통째로 한 번 그려져서 '전체가 번쩍' 보인다. data-done 은 서버
   HTML 에 이미 박혀 오므로 CSS 는 요소가 들어오는 즉시(첫 페인트 전에) 걸린다.
   JS 는 순서가 된 말풍선에만 인라인 display 를 넣어 되살린다. */
.chat[data-done="1"] > .turn { display: none; }
.turn { display: flex; flex-direction: column; gap: 3px; max-width: 91%; }
.turn.caller { align-self: flex-end; align-items: flex-end; }
.turn.plain { max-width: 100%; align-self: stretch; }
.who {
    display: flex; align-items: center; gap: 5px;
    font-size: 15px; font-weight: 750; color: var(--lp-text); padding: 0 5px;
}
/* 민원인은 오른쪽 정렬이라 아이콘도 바깥쪽(오른쪽)으로 보낸다 — 말풍선 꼬리 방향과 맞춘다 */
.turn.caller .who { flex-direction: row-reverse; }
.who-ic { font-size: 15px; line-height: 1; }
.bubble {
    /* 타이핑 중에 최종 크기를 min-width/min-height 로 미리 잡아 두므로(JS typeChats),
       그 값이 테두리·패딩까지 포함한 크기와 맞아야 한다 → border-box 로 못박는다. */
    box-sizing: border-box;
    padding: 9px 13px; border-radius: 15px; line-height: 1.65; font-size: 17px;
    background: var(--lp-bub-you); border: 1px solid var(--lp-bub-you-bd);
    color: var(--lp-text); white-space: pre-wrap; word-break: break-word;
}
.turn.agent  .bubble { border-bottom-left-radius: 5px; }
.turn.caller .bubble {
    border-bottom-right-radius: 5px;
    background: var(--lp-bub-me); border-color: var(--lp-bub-me-bd);
}

/* ── 변경 단어 태그 박스 (체크 버튼으로 켜는 방식) ────────────────────
   서버(lifespan.py)는 색을 박지 않고 표식만 준다: data-diff 에 "r"(STT↔보정 변경)/
   "b"(보정↔표준어 변경)/"rb"(둘 다) 가 들어온다. 색은 여기서, 켜진 모드에 따라 입힌다.
   - 컨테이너에 .mode-cor 면 r(과 rb) 를 빨강 배지로  → STT·보정 칸이 함께 빨강
   - 컨테이너에 .mode-nor 면 b(과 rb) 를 파랑 배지로  → 보정·표준어 칸이 함께 파랑
   두 모드는 상호배타(JS)라 한 단어가 빨강과 파랑을 동시에 갖는 일은 없다(보라 불필요).
   .diffbox *:not([data-lp]) 리셋이 배경을 지우므로 더 높은 특이도 + !important 로 덮는다. */
/* 두 전환 모두 **빨강 한 색**으로 통일한다 — 어차피 체크 버튼으로 한 번에 하나만 보므로
   색을 나눌 이유가 없고, 한 색이면 '바뀐 곳'이라는 뜻이 더 또렷하다. */
.mode-cor .diffbox .bubble span[data-diff*="r"],
.mode-nor .diffbox .bubble span[data-diff*="b"] {
    display: inline; padding: 1px 7px !important; border-radius: 7px !important;
    font-weight: 700 !important;
    /* slice(기본값) 로 둔다. clone 이면 한 덩어리가 줄바꿈될 때 줄마다 좌우 여백·둥근
       모서리가 새로 그려져 **한 span 인데 박스 두 개로 갈라져 보인다**("몬 살겠어예" 가
       "몬 살겠" + "어예" 로 보이던 원인). slice 는 진짜 시작·끝에만 모서리를 줘서
       줄을 넘어가도 한 박스로 이어져 보인다. */
    -webkit-box-decoration-break: slice; box-decoration-break: slice;
    color: #d03b3b !important;
    background: rgba(208,59,59,.13) !important;
    box-shadow: inset 0 0 0 1px rgba(208,59,59,.32) !important;
}
.dark .mode-cor .diffbox .bubble span[data-diff*="r"],
.dark .mode-nor .diffbox .bubble span[data-diff*="b"] { color: #f1948a !important; }

/* ── 실시간 변경 태그 (자동) ─────────────────────────────────────────
   체크 버튼(.mode-*)은 "다 끝난 뒤 눌러서 확인"하는 기능이다. 이건 그것과 **별개**로,
   말풍선이 타이핑되는 동안 바뀐 단어에 박스가 그려지는 기능이다. 두 칸에 동시에 걸린다:
     .live-cor → STT 칸(r) + 보정 칸의 r 판본      (STT → 사투리 보정 전환)
     .live-nor → 보정 칸의 b 판본 + 표준어 칸(b)   (사투리 보정 → 표준어 전환)
   색·모양은 위 .mode-* 와 **똑같이** 맞춘다 — 같은 뜻(바뀐 곳)을 두 가지 모양으로 보이게
   하면 사용자가 다른 의미로 읽는다. 다른 건 '언제 켜지는가'뿐이다.
   체크 버튼이 켜져 있으면 JS 가 이걸 아예 안 건다(수동 조작이 우선). */
.live-cor .diffbox .bubble span[data-diff*="r"].lv,
.live-nor .diffbox .bubble span[data-diff*="b"].lv {
    display: inline; padding: 1px 7px !important; border-radius: 7px !important;
    font-weight: 700 !important;
    -webkit-box-decoration-break: slice; box-decoration-break: slice;
    color: #d03b3b !important;
    background: rgba(208,59,59,.13) !important;
    box-shadow: inset 0 0 0 1px rgba(208,59,59,.32) !important;
    /* 글자가 처음 보이는 순간 박스가 그려진다. twRender 가 이 span 을 display:none →
       inline 으로 바꾸는데, display 가 바뀌면 애니메이션이 처음부터 다시 재생된다(명세
       동작). 그래서 JS 로 시점을 따로 잡지 않아도 '그 단어가 나오는 순간'과 정확히 맞는다. */
    animation: boxdraw .34s cubic-bezier(.22,.9,.3,1) both;
}
.dark .live-cor .diffbox .bubble span[data-diff*="r"].lv,
.dark .live-nor .diffbox .bubble span[data-diff*="b"].lv { color: #f1948a !important; }

/* 박스가 '쳐지는' 모양: 테두리가 먼저 또렷하게 들어왔다가 제자리로 가라앉는다.
   layout 속성(width/height/padding)은 건드리지 않는다 — 인라인 글자가 밀리면 읽는
   중에 줄이 흔들린다. 칠(paint) 속성만 움직이므로 재배치가 일어나지 않는다. */
@keyframes boxdraw {
    0%   { background-color: rgba(208,59,59,0);   box-shadow: inset 0 0 0 1px rgba(208,59,59,0); }
    55%  { background-color: rgba(208,59,59,.24); box-shadow: inset 0 0 0 2px rgba(208,59,59,.62); }
    100% { background-color: rgba(208,59,59,.13); box-shadow: inset 0 0 0 1px rgba(208,59,59,.32); }
}

/* 모션을 줄이도록 설정한 사용자에게는 움직임만 없앤다 — 박스 자체는 그대로 보인다.
   (정보를 빼는 게 아니라 애니메이션만 끄는 것) */
@media (prefers-reduced-motion: reduce) {
    .live-cor .diffbox .bubble span[data-diff*="r"].lv,
    .live-nor .diffbox .bubble span[data-diff*="b"].lv { animation: none !important; }
}

/* ── 보정 칸의 두 판본 전환 ───────────────────────────────────────────
   서버가 보정문을 '보정 체크용(r 표식)'과 '표준어 체크용(b 표식)' 두 벌로 준다.
   여기 CSS 는 **JS 가 돌기 전 초기값**만 잡는다(기본 = 보정 판본). 실제 전환은 JS
   (_MODE_BODY 의 applyViews)가 인라인 style 로 한다 — Gradio 가 커스텀 CSS 에 스코프를
   붙이면서 `.mode-nor .cview-nor` 같은 규칙이 안 먹는 것을 실측으로 확인했다
   (선택자는 매치되는데 display 가 안 바뀜). 인라인 style 은 스코프 영향을 안 받는다. */
.cview-nor { display: none; }

/* ── 체크 버튼 (섹션 칩 옆) ──────────────────────────────────────────
   .chip-row 는 gr.HTML 내부의 일반 div (섹션칩 + 버튼을 한 줄에). Gradio Row 가 아니다. */
/* flex:0 0 auto 가 핵심이다. Gradio 블록의 기본값은 flex-grow:1 이라, 칸에 남는 세로를
   칩 줄과 아래 박스가 반씩 나눠 가진다 — 그래서 칩 아래에 28px 짜리 빈 공간이 생기고,
   그 크기가 '남는 공간이 있느냐'에 따라 칸마다 달라져서 네 칸의 박스 윗선이 어긋났다
   (실측: 텍스트 3칸은 칩줄 83px, 키워드 칸은 53px → 박스 top 263 vs 233).
   칩 줄을 내용 높이에 고정하면 네 칸 모두 같은 높이가 되고 박스 윗선이 정확히 맞는다. */
.chip-line { min-width: 0 !important; flex: 0 0 auto !important; }
/* 칩은 왼쪽, 버튼은 그 칸 오른쪽 끝으로 (space-between) */
.chip-row { display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; justify-content: space-between; }
.chip-row .section-chip { flex: 0 1 auto; }
.diff-toggle {
    flex: 0 0 auto;
    display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
    padding: 4px 11px; border-radius: 999px; cursor: pointer;
    font-size: 13px; font-weight: 700;
    background: var(--lp-idle-bg); color: var(--lp-dim);
    border: 1px solid transparent; transition: background .15s, color .15s, border-color .15s;
}
.diff-toggle::before {   /* 체크박스 모양 */
    content: ""; width: 14px; height: 14px; border-radius: 4px; flex: none;
    border: 1.6px solid currentColor; opacity: .5;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 11px; line-height: 1; font-weight: 900;
}
/* 켜진 상태: 각 모드 색(빨강/파랑) + 체크 표시. 컨테이너의 .mode-* 로 결정된다.
   !important 필수 — Gradio 가 기본 .diff-toggle 규칙을 스코프(.contain 접두)해서 특이도를
   높여버리므로, important 없으면 회색 기본 스타일에 눌려 체크 표시가 안 뜬다. */
/* 강조색을 빨강 하나로 통일했으므로 두 버튼의 켜진 색도 같다 */
.mode-cor .diff-toggle[data-mode="cor"],
.mode-nor .diff-toggle[data-mode="nor"] {
    border-color: currentColor !important;
    color: #c0392b !important; background: rgba(208,59,59,.13) !important;
}
.mode-cor .diff-toggle[data-mode="cor"]::before,
.mode-nor .diff-toggle[data-mode="nor"]::before { content: "✓" !important; opacity: 1 !important; }
.dark .mode-cor .diff-toggle[data-mode="cor"],
.dark .mode-nor .diff-toggle[data-mode="nor"] { color: #f1948a !important; }
.diffbox::-webkit-scrollbar { width: 10px; }
.diffbox::-webkit-scrollbar-thumb { background: var(--lp-idle-dot); border-radius: 99px;
    border: 3px solid transparent; background-clip: content-box; }
.placeholder { color: var(--lp-dim); font-size: 15.5px; }

/* ── 키워드: 표준어 변환 옆 얇은 카드 (같은 행의 4번째 칸) ─────────────
   생김새를 옆 3칸과 똑같은 구조로 맞춘다 — 넷이 한 세트로 읽히게:
       옆 3칸 : 칩 → 인셋 박스(.diffbox) → [.who 라벨 + .bubble 말풍선] × N
       키워드 : 칩 → 인셋 박스(.kw-list) → [.kw-label      + .kw-value ] × 5
   그래서 인셋 배경·테두리·모서리와 라벨/말풍선 스타일을 대화 쪽 값과 같이 쓴다.
   (이전의 좌측 굵은 색 막대 타일은 다른 카드에 없는 장식이라 뺐다) */
.pane-kw { gap: 10px !important; }

/* 인셋 프레임: .diffbox 와 같은 배경·테두리·모서리·패딩 리듬.
   높이도 .diffbox 와 **똑같이** 잡는다 (아래 height/flex). 여기서 flex:1 로 늘어나게 두면
   이 칸이 행 높이를 혼자 붙잡아, FIT_JS 가 텍스트창을 줄여도 컨테이너가 안 줄어든다
   → 넘침 계산이 매 틱 커져서 텍스트창이 최소치(160px)까지 쭈그러든다(실측으로 확인).
   내용이 넘치면 이 안에서만 스크롤한다(행 높이는 안 늘어남). */
.kw-list {
    display: flex !important; flex-direction: column;
    /* Gradio Column 기본 정렬이 가운데면 위쪽에 빈 칸이 생겨 옆 칸과 시작선이 어긋난다 */
    justify-content: flex-start !important; align-items: stretch !important;
    gap: 9px;
    height: calc(100vh - 430px) !important; min-height: 160px !important;
    flex: 0 0 auto !important;
    overflow: auto !important; overscroll-behavior: contain; box-sizing: border-box;
    padding: 14px 13px !important;
    background: var(--lp-inset) !important;
    border: 1px solid var(--lp-border) !important;
    border-radius: 12px !important;
    box-shadow: none !important;
}
/* ── 키워드 카드 위쪽의 위치 지도 ─────────────────────────────────────
   지도는 '키워드 5개를 뺀 남은 세로'를 전부 차지한다(flex:1). 키워드는 자기 높이만
   먹고(flex:0) 카드 아래쪽에 모인다.
   마커는 .mapwrap(컨테이너) 기준 % 좌표다. 그래서 이미지를 object-fit:fill 로 컨테이너에
   꽉 채워야(=이미지 x% ↔ 컨테이너 x%) 마커가 정확히 얹힌다 — cover 로 자르면 어긋난다.
   (요청 비율을 세로로 길게 잡아 늘어남을 최소화한다) */
.kw-map-slot {
    /* 자기 내용(범례 + 지도 비율) 높이만 차지한다 — 남은 세로는 키워드 칸(.kw-kwbox)이 받는다.
       flex:1 로 두면 이 칸이 남은 세로를 다 잡아 놓고 그 안에서 지도가 비율만큼만 그려져
       칸 아래쪽이 빈 채로 남았다(실측: 칸 312 / 지도 317 로 서로 어긋났다). */
    flex: 0 0 auto !important; min-height: 130px !important;
    display: flex !important; flex-direction: column !important;
    min-width: 0 !important; padding: 0 !important; margin: 0 !important; border: 0 !important;
    background: transparent !important; box-shadow: none !important;
}
/* 지도를 아예 못 받은 경우(둘 다 빈 문자열) → 이 칸을 접어 키워드만 보이게 한다. */
.kw-map-slot:not(:has(.mapwrap)) { display: none !important; flex: 0 0 auto !important; }
/* Gradio 가 HTML 을 감싸는 래퍼(.html-container/.prose)들이 높이를 안 넘겨주면 지도가
   안 늘어난다 → 이 칸 안에서는 전부 세로로 늘어나는 flex 통로로 만든다. */
.kw-map-slot > *, .kw-map-slot .html-container, .kw-map-slot .prose {
    display: flex !important; flex-direction: column !important;
    flex: 1 1 auto !important; min-height: 0 !important; width: 100% !important;
    padding: 0 !important; margin: 0 !important; border: 0 !important; background: transparent !important;
}
/* 범례 칸은 자기 높이만 (위 `> *` 규칙의 flex:1 을 되돌린다 — 안 그러면 지도와 공간을 나눈다).
   ※ 이 두 규칙은 특이도가 위 `.kw-map-slot > *` 와 같으므로 **뒤에** 와야 이긴다. */
.kw-legend-slot { flex: 0 0 auto !important; }
/* 확대·마커 지도 칸은 기본 숨김. 표준어 변환 대화가 끝나면 JS 가 이 칸을 켜고 전국 지도
   칸을 껀다 (확대·마커는 '키워드 추출 결과'와 한 세트다). JS 가 안 돌면 전국 지도가 계속
   보이는 쪽이 안전하다. 값이 중간에 채워져도 숨은 칸이라 화면에는 변화가 없다. */
.kw-map-zoom { display: none !important; }
/* 내용(.mapwrap)이 없는 지도 칸은 자리를 차지하지 않게 접는다 */
.kw-mapview:not(:has(.mapwrap)) { display: none !important; flex: 0 0 auto !important; }
/* 지도 칸은 '남은 세로 전부'가 아니라 **지도 비율만큼만** 차지한다 (위 `.kw-map-slot > *` 의
   flex:1 을 되돌린다). 남은 세로를 다 먹으면 정작 이미지는 폭에 맞춰 작게 그려지고 그 차액이
   위아래 빈 칸으로 남았다. 지금은 폭이 허용하는 최대 높이(= 폭 ÷ 비율)까지 지도가 꽉 찬다. */
.kw-mapview:has(.mapwrap) { flex: 0 0 auto !important; }
/* 지도 감싸는 박스(테두리·배경) 없이 지도 이미지만. 마커 위치 기준(position:relative)과
   지도 밖으로 나간 마커를 자르는 overflow:hidden 은 남긴다. 모서리만 살짝 둥글게.
   aspect-ratio: 받아온 이미지와 같은 비율로 박스를 잡아 letterbox(빈 여백)를 없앤다.
   max-height: 창이 아주 낮을 때만 걸린다 — 그 경우엔 contain 이 알아서 줄인다(안 잘린다). */
.mapwrap {
    position: relative; flex: 0 0 auto; width: 100%; box-sizing: border-box;
    aspect-ratio: 420 / 560; max-height: 100%; min-height: 0;
    border: 0 !important; background: transparent !important;
    border-radius: 8px !important; overflow: hidden; line-height: 0;
}
/* contain: 남은 공간 안에 이미지를 왜곡·크롭 없이 최대로. 남는 여백(letterbox)은
   지도색과 이어지는 어두운 바탕으로 둔다. 마커는 JS 가 실제 이미지 영역 위에 얹는다. */
.mapimg { display: block; width: 100%; height: 100%; object-fit: contain; }
.mapwrap-load {                          /* 이미지 받는 동안의 자리표시 */
    display: flex; align-items: center; justify-content: center;
    line-height: 1.4; font-size: 12.5px; color: var(--lp-dim);
}
/* 키워드 묶음: 지도가 자기 비율만큼 가져간 뒤 **남은 세로를 이 칸이 받는다**(flex:1).
   다섯 줄을 그 안에 고르게 펼치므로(space-between) 남는 공간이 '빈 칸'이 아니라 줄 간격으로
   보인다. 값이 길어 모자라면 이 안에서만 스크롤한다(지도를 밀어내지 않는다). */
.kw-kwbox {
    flex: 1 1 auto !important; min-height: 0 !important;
    display: flex !important; flex-direction: column !important;
    /* gap 은 '최소' 간격일 뿐이다 — 남는 세로가 있으면 space-between 이 더 벌려 준다.
       8px 로 두면 다섯 줄 최소 높이가 칸보다 8px 커져서 스크롤이 생겼다(실측). */
    justify-content: space-between !important; gap: 4px !important;
    overflow: auto !important; overscroll-behavior: contain;
    padding: 0 !important; border: 0 !important; background: transparent !important;
    box-shadow: none !important;
}
.kw-kwbox::-webkit-scrollbar { width: 7px; }
.kw-kwbox::-webkit-scrollbar-thumb { background: var(--lp-idle-dot); border-radius: 99px;
    border: 2px solid transparent; background-clip: content-box; }
/* 마커는 좌표 위에 정확히 얹히는 '원'으로 한다 — 물방울 핀은 꼭지 보정이 필요해 어긋난다 */
.mpin {
    position: absolute; left: 0; top: 0; width: 14px; height: 14px; border-radius: 50%;
    transform: translate(-50%, -50%);
    border: 2.5px solid #fff; box-shadow: 0 1px 4px rgba(0,0,0,.55);
    visibility: hidden;                  /* JS(placeMapPins)가 자리 잡은 뒤 보이게 — 좌상단 깜빡임 방지 */
}
.mpin-rep  { background: #2b49ad; }      /* 신고자 = 파랑 (STT 칩 색 계열) */
.mpin-odor { background: #d03b3b; }      /* 냄새   = 빨강 (변경 표시와 같은 빨강) */
/* 범례: 지도 바로 위 가로 한 줄 (지도 밖이라 마커와 안 겹친다). 카드 배경 위에 놓이므로
   글씨는 본문색, 점만 마커 색. flex:0 로 자기 높이만 먹고 지도는 그 아래로 채운다. */
.maplegend {
    flex: 0 0 auto !important; line-height: 1; margin-bottom: 7px;
    /* 지도 칸을 세로 flex 통로로 만든 위 .kw-map-slot 규칙이 이 안까지 세로로 세워서
       범례가 두 줄로 쌓였다 → 여기서 가로(row)로 못박는다. */
    display: flex !important; flex-direction: row !important;
    gap: 10px; align-items: center; justify-content: center;
    flex-wrap: nowrap; white-space: nowrap;
    /* 지도 텍스트 창(.diffbox)과 같은 인셋 박스로 감싼다 */
    padding: 6px 10px !important;
    background: var(--lp-inset) !important;
    border: 1px solid var(--lp-border) !important; border-radius: 10px !important;
}
.lg {
    display: inline-flex; align-items: center; gap: 5px; flex: none;
    /* 범례는 nowrap 이라 넘치면 잘린다. 키워드 칸의 최소 폭(min_width=300 → 안쪽 약
       248px)에 두 항목이 다 들어가야 한다: 글자 2×74 + 점 2×9 + 간격들 ≈ 200px.
       14px 이 여유를 남기는 상한이다. 더 키우려면 min_width 부터 올려야 한다. */
    font-size: 14px; font-weight: 700; color: var(--lp-text);
}
.lg::before { content: ""; width: 9px; height: 9px; border-radius: 50%; flex: none; }
.lg-rep::before  { background: #2b49ad; }
.lg-odor::before { background: #d03b3b; }
.lg-sep { flex: none; color: var(--lp-idle-dot); font-weight: 400; }

/* gr.HTML 래퍼(블록+prose 양쪽에 클래스가 붙는다)는 투명하게 접어 두께를 안 만든다 */
.kw-slot {
    flex: 0 0 auto !important; display: block !important; min-width: 0 !important;
    padding: 0 !important; margin: 0 !important; border: 0 !important;
    background: transparent !important; box-shadow: none !important;
}
/* Gradio 가 래퍼 안에 한 겹 더 끼우는 .html-container 는 위아래 10px 패딩을 갖는다.
   키워드 다섯 줄이면 100px 이 그냥 빈 공간으로 날아간다(실측: 한 줄 79px 중 내용은 59px).
   그만큼 지도가 못 커지고 줄 간격도 벌어져 카드가 헐렁해 보였다 → 이 칸에서만 걷어낸다. */
.kw-slot .html-container { padding: 0 !important; }
/* 새 값이 들어오면 살짝 떠오르며 강조 */
@keyframes kwIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: none; } }
.kw {
    width: 100%; box-sizing: border-box;
    display: flex; flex-direction: column; gap: 4px;
    padding: 9px 2px;
    animation: kwIn .26s ease both;
}
/* 첫 항목 위에는 선을 긋지 않는다 (칩 바로 아래에 줄이 두 개로 보인다).
   인접 형제(+) 대신 :not(:first-child) 를 쓴다 — Gradio 가 칸 사이에 다른 노드를
   끼우면 + 는 끊기지만 이건 버틴다. */
.kw-slot:not(:first-child) .kw { border-top: 1px solid var(--lp-border); }
/* 항목별 색은 여기서 한 번만 묶는다 — 배지 CSS 는 --kw-* 만 읽는다 */
.kw-i0 { --kw-fg: var(--kw-c0-fg); --kw-bg: var(--kw-c0-bg); --kw-bd: var(--kw-c0-bd); }
.kw-i1 { --kw-fg: var(--kw-c1-fg); --kw-bg: var(--kw-c1-bg); --kw-bd: var(--kw-c1-bd); }
.kw-i2 { --kw-fg: var(--kw-c2-fg); --kw-bg: var(--kw-c2-bg); --kw-bd: var(--kw-c2-bd); }
.kw-i3 { --kw-fg: var(--kw-c3-fg); --kw-bg: var(--kw-c3-bg); --kw-bd: var(--kw-c3-bd); }
.kw-i4 { --kw-fg: var(--kw-c4-fg); --kw-bg: var(--kw-c4-bg); --kw-bd: var(--kw-c4-bd); }
/* ── 키워드 항목의 시선 순서 ──────────────────────────────────────────
   예전에는 라벨이 값보다 진했고(750 vs 600) 값이 15px 이라, 다섯 칸이 전부
   똑같은 흰 말풍선으로 보여 '무엇이 추출됐는지'가 눈에 안 들어왔다. 순서를 뒤집는다:
     아이콘 배지(항목 식별) → 값(결과, 가장 크고 진하게) → 라벨(보조)
   말풍선을 걷어낸 것도 같은 이유다 — 옆 대화 칸의 말풍선과 모양이 같아서 서로 묻혔다.
   항목 사이는 얇은 선으로만 나눈다(좁은 칸이라 여백보다 선이 경제적이다). */
.kw-head { display: flex; align-items: center; gap: 8px; }
/* 아이콘 배지. 크기(26px)는 고정 — 값 글자가 길어져도 흔들리지 않게 flex 를 잠근다. */
.kw-ico {
    flex: 0 0 auto; width: 26px; height: 26px; border-radius: 9px;
    display: inline-flex; align-items: center; justify-content: center;
    background: var(--kw-bg); color: var(--kw-fg);
    border: 1px solid var(--kw-bd);
}
.kw-ico svg { width: 16px; height: 16px; display: block; }
/* 라벨 = 보조 정보로 내린다 (예전엔 여기가 제일 진했다).
   ★ 크기는 올려도 값(17px/700)보다는 확실히 작게 유지한다 — 둘이 비슷해지면 계층이
     도로 무너져서 '무엇이 추출됐는지'가 다시 안 보인다. 14px/650 이 그 경계다. */
.kw-label { font-size: 14px; font-weight: 650; color: var(--lp-dim); padding: 0; }
/* 값 = 이 칸의 주인공. 배지 폭(26)+gap(8)만큼 들여써 라벨과 같은 세로선에 맞춘다. */
.kw-value {
    padding: 0 0 0 34px;
    color: var(--lp-text); font-size: 17px; font-weight: 700; line-height: 1.4;
    /* 좁은 칸이라 줄바꿈이 자주 생긴다. keep-all 이면 한국어를 어절(띄어쓰기) 단위로 끊어
       '베러플레이스키즈 / 풀빌라' 처럼 읽히고, 띄어쓰기 없는 긴 낱말만 anywhere 로 쪼갠다. */
    word-break: keep-all; overflow-wrap: anywhere;
}
/* '미언급'·빈 값은 흐리게 — 채워진 항목이 먼저 눈에 들어오게. 배지도 같이 가라앉힌다
   (색이 살아 있으면 빈 항목이 채워진 항목만큼 시선을 끈다). */
.kw-value.dim { color: var(--lp-dim); font-weight: 500; font-size: 15px; }
.kw.dim .kw-ico { background: var(--lp-idle-bg); color: var(--lp-idle-fg);
    border-color: var(--lp-idle-dot); }
.kw-list::-webkit-scrollbar { width: 8px; }
.kw-list::-webkit-scrollbar-thumb { background: var(--lp-idle-dot); border-radius: 99px;
    border: 2px solid transparent; background-clip: content-box; }

/* ── 알림 토스트 ─────────────────────────────────────── */
/* 빈 알림 홀더 접기. min-height:0 만으로는 20px 이 남아 있었다(실측) → height 를 0 으로
   못박고 넘치게 둔다. 토스트는 position:fixed 라 홀더 크기와 무관하게 화면에 뜬다. */
.notice-holder {
    height: 0 !important; min-height: 0 !important; overflow: visible !important;
    padding: 0 !important; margin: 0 !important; border: 0 !important;
}
.toast-notice {
    position: fixed; right: 22px; top: 22px; z-index: 9999;
    display: inline-flex; align-items: center; gap: 10px;
    padding: 13px 20px; border-radius: 14px;
    font-size: 16.5px; font-weight: 700; color: #fff; background: var(--lp-run-dot);
    box-shadow: 0 12px 30px rgba(16,24,40,.28);
    animation: toastin .34s cubic-bezier(.2,.9,.3,1.15);
}
.toast-notice.done { background: var(--lp-done-dot); }
.toast-notice.err  { background: var(--lp-err-dot); }
@keyframes toastin {
    from { opacity: 0; transform: translateY(-14px) scale(.97); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
}
"""


def fmt_sec(sec, done: bool) -> str:
    """경과 시간 표기. 진행 중엔 정수 초로만 보여준다 — 0.1초 단위로 바꾸면 폴링마다
    값이 달라져 이 컴포넌트가 초당 10회 갱신된다(초당 1회로 줄인다). 끝나면 소수 첫째
    자리까지 보여준다(시연에서 '몇 초 걸렸나'가 결과값이므로)."""
    if sec is None:
        return ""
    if sec < 60:
        return f"{sec:.1f}초" if done else f"{int(sec)}초"
    m, s = divmod(sec, 60)
    return f"{int(m)}분 {s:.0f}초"


def progress_html(step: int, error=None, file: str = "", elapsed=None) -> str:
    """제목 아래 줄: 오류가 있을 때만 오류 문구. 평소엔 아무것도 안 그린다.

    진행 막대·8단계 칩 줄·파일명·단계 이름·경과 시간을 다 뺐다. 막대와 단계 이름은
    **서버 단계**를 따라가는데 아래 섹션 칸의 반짝임은 **화면에 보이는 진행**(타이핑이 끝난
    칸까지)을 따라가므로, 나란히 두면 서로 어긋나 보여 헷갈린다. 어디까지 나왔는지는 칸의
    반짝임으로 읽는다.
    step/file/elapsed 인자는 호출부를 그대로 두려고 남겨 두었다 — 화면에는 안 쓴다.
    """
    return f'<div class="rail-err">오류: {_html.escape(str(error))}</div>' if error else ""


# 아래 섹션 칩이 STEPS 의 어느 구간 동안 '진행 중'인지 (인덱스 lo..hi, 양끝 포함).
# 사투리 보정은 시/군 추정~잔여 보정(1..5) 다섯 단계를 거쳐 corrected_final 이 확정된다.
SECTIONS = [
    ("STT 결과", 0, 0),
    ("사투리 보정", 1, 5),
    ("표준어 변환", 6, 6),
    ("키워드 추출 결과", 7, 7),
]

# 텍스트 창이 빌 때 보여줄 안내 (빈 회색 박스 세 개만 떠 있지 않도록)
PLACEHOLDERS = [
    "음성이 업로드되면 STT 결과가 여기에 표시됩니다.",
    "사투리 보정 결과가 여기에 표시됩니다. '변경 표시'를 켜면 STT 대비 바뀐 부분이 빨갛게 표시됩니다.",
    "표준어 변환 결과가 여기에 표시됩니다. '변경 표시'를 켜면 사투리 보정 대비 바뀐 부분이 빨갛게 표시됩니다.",
]


def section_html(label: str, lo: int, hi: int, step: int, error=None, sec: int = 0) -> str:
    """sec: 칸 번호(0~3). 칸마다 고유한 색을 쓰라고 data-sec 으로 넘긴다 (CSS 가 색 결정).
    상태(대기/진행/완료/오류)는 그 색의 '세기'로 나타낸다 — 대기는 회색, 오류는 빨강."""
    if step < 0 or step < lo:
        cls = ""
    elif error is not None and step <= hi:
        cls = "error"
    elif step > hi:
        cls = "done"
    else:
        cls = "running"
    return f'<span class="section-chip {cls}" data-sec="{sec}">{label}</span>'


# 사투리 보정(1)·표준어 변환(2) 칩 옆에 붙는 '변경 표시' 체크 버튼. 정적 마크업이라
# 폴링마다 다시 그려도 무방하다 — 클릭은 document 위임, 체크 표시는 컨테이너 .mode-* 로 결정.
_TOGGLE_BTN = {
    1: '<button class="diff-toggle" data-mode="cor" type="button" '
       'title="STT → 사투리 보정 변경 표시">변경 표시</button>',
    2: '<button class="diff-toggle" data-mode="nor" type="button" '
       'title="사투리 보정 → 표준어 변환 변경 표시">변경 표시</button>',
}


def section_block(i: int, step: int, error=None) -> str:
    """칩 + (해당되면) 체크 버튼을 한 flex 줄(.chip-row)로. 하나의 gr.HTML 로 넣어야
    Gradio Row 가 폭을 반씩 갈라 칩 글자가 세로로 접히는 문제가 안 생긴다."""
    label, lo, hi = SECTIONS[i]
    chip = section_html(label, lo, hi, step, error, sec=i)
    btn = _TOGGLE_BTN.get(i, "")
    return f'<div class="chip-row">{chip}{btn}</div>' if btn else chip


# 서버가 준 내용 HTML 은 "상담원: …" / "민원인: …" 줄이 빈 줄로 구분된 형태다.
# diff 강조 span 은 한 줄 안에서만 생기므로(공백 토큰은 강조 대상이 아니다) 줄 단위로
# 잘라도 태그가 끊기지 않는다 → UI 에서 안전하게 말풍선으로 감쌀 수 있다.
_SPEAKER_RE = re.compile(r"^(상담원|민원인)\s*[:：]\s*")
_SIDE = {"상담원": "agent", "민원인": "caller"}
# 화자 라벨 옆 아이콘. 글씨는 그대로 두고 앞에 하나만 붙인다(아이콘만 쓰면 누가 누군지
# 헷갈린다). 말풍선 방향과 맞춰 상담원은 왼쪽, 민원인은 오른쪽 바깥에 오도록 CSS 가 뒤집는다.
# 민원인: 유니코드에 '코를 막은 사람' 이모지는 없다. 악취 민원이라는 맥락이 바로 읽히는
# 🤢(속이 안 좋은 얼굴)로 대신한다. (대안: 😷 마스크 / 🤧 재채기 / 🙋 손 든 사람)
_WHO_ICON = {"상담원": "🎧", "민원인": "😷"}


def chat_html(body: str, job: str = "", done: bool = False) -> str:
    """내용 HTML → 대화 말풍선. diff span 은 손대지 않으므로 빨강/파랑 강조는 그대로 남는다.
    화자 라벨이 없는 줄(화자분리 실패·원문 그대로)은 라벨 없는 전체폭 말풍선으로 그린다.
    data-lp: .diffbox 안 Gradio 래퍼 초기화 규칙에서 우리 요소를 빼내는 표식.
    data-job: 스크롤 위치 판단용 — 이 값이 바뀌면 '새 민원'이라 JS 가 맨 위로 되돌린다."""
    turns = []
    for line in (body or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _SPEAKER_RE.match(line)
        who = m.group(1) if m else ""
        side = _SIDE.get(who, "plain")
        label = (f'<div class="who" data-lp>'
                 f'<span class="who-ic" data-lp>{_WHO_ICON.get(who, "")}</span>{who}</div>'
                 ) if who else ""
        turns.append(f'<div class="turn {side}" data-lp>{label}'
                     f'<div class="bubble" data-lp>{line[m.end():] if m else line}</div></div>')
    # data-done: 그 칸의 단계가 끝난 최종 대화라는 표식. JS(typeChats)가 이걸 보고
    # 말풍선을 위에서부터 타이핑해 띄운다(처리 중에는 표식이 없어 재생하지 않는다).
    done_attr = ' data-done="1"' if done else ""
    return (f'<div class="chat" data-lp data-job="{_html.escape(job)}"{done_attr}>'
            f'{"".join(turns)}</div>' if turns else "")


# ── 위치 지도 (키워드 카드 맨 위) ──────────────────────────────────────────
# 네이버 정적지도 이미지를 받아 data URI 로 박고, 마커는 우리가 HTML 로 얹는다.
#
# ■ 왜 마커를 직접 얹나
#   정적지도 API 는 마커 라벨을 **마지막 하나만** 그리고 color 파라미터는 무시한다(실측).
#   그러면 신고자/냄새 두 지점이 똑같은 빨간 핀이 되어 구분이 안 된다.
#
# ■ 좌표 → 픽셀
#   웹 메르카토르이고, 네이버의 level N 은 표준 타일 zoom N+1 이다.
#   (네이버가 그린 마커 픽셀 위치와 대조해 검증: 두 점 거리 비율 1.0000, 오차 ~1px)
#   마커는 % 로 배치하므로 이미지가 카드 폭에 맞춰 늘어나도 위치가 안 어긋난다.
# 요청 크기(CSS px). 지도는 '키워드를 뺀 남은 세로'를 다 채우도록 카드 안에서 늘어나므로
# (아래 CSS: .mapwrap{flex:1}), 여기 값은 정적지도 이미지의 요청 해상도일 뿐이다. 카드가
# 세로로 기니 세로로 넉넉한 비율로 받는다(작게 받아 늘리면 흐려지므로).
# ※ 좌표→픽셀 투영은 이 요청 크기 기준이라, 표시 크기와 별개로 마커 위치는 정확하다.
# 세로로 긴 비율(3:4)로 받는다. 키워드 카드는 좁고 길어서, 정사각으로 받으면 contain 이
# 가로 폭에 맞춰 축소되고 위아래에 큰 여백(letterbox)이 남았다 — 지도가 작고 빈 칸이 크게
# 느껴졌다(실측: 238×271 칸에 227×227 만 그려짐). 아래 CSS 가 지도 칸을 이 비율로 붙잡으므로
# (aspect-ratio) 여백 없이 폭이 허용하는 최대 높이까지 지도가 채운다.
_MAP_W, _MAP_H = 420, 560
_MAP_SCALE = 2                     # 2배 해상도로 받아 축소 표시 → 선명
_MAP_TYPE = "satellite"            # 위성(라벨 포함). 일반지도는 "basic"
# 민원이 들어오면(추출 전) 곧바로 띄우는 '빈 지도'. 좌표가 아직 없을 땐 **대한민국 전국**을
# 보여주고(고정 중심·줌 — 지오코딩 없이 항상 즉시 뜬다), 마커가 나오면 두 점에 맞춰 확대한다.
_MAP_DEFAULT_CENTER = (127.9, 36.3)   # 대한민국 중앙 (경도, 위도)
_MAP_DEFAULT_LEVEL = 6             # 전국이 한눈에 들어오는 줌 (실측)
_MAP_TIMEOUT = 8.0
# 지도 키는 환경변수로만 받는다. 없으면 지도만 안 뜨고 나머지는 정상 동작한다.
_NCP_KEY_ID = os.environ.get("NCP_GEOCODE_KEY_ID", "")
_NCP_KEY = os.environ.get("NCP_GEOCODE_KEY", "")
_STATIC_MAP_URL = "https://maps.apigw.ntruss.com/map-static/v2/raster"

# 받아온 지도 이미지 캐시. 같은 (중심, 레벨) 이면 다시 안 받는다.
# 폴링(1초)이 도는 함수 안에서 네트워크를 타면 화면이 멈추므로, 실제 요청은 별도 스레드가
# 하고 poll 은 캐시만 본다 (준비되기 전에는 자리표시만 보여준다).
_map_cache = {}
_map_pending = set()
_map_lock = threading.Lock()


def _mercator(lon: float, lat: float, level: int):
    """(lon, lat) → 전세계 픽셀 좌표. level 은 네이버 정적지도 level (= 표준 zoom - 1)."""
    world = 256 * (2 ** (level + 1))
    x = (lon + 180.0) / 360.0 * world
    s = math.sin(math.radians(max(-85.0, min(85.0, lat))))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * world
    return x, y


def _pick_level(pts) -> int:
    """두 점이 프레임 가운데 70% 안에 들어오는 가장 확대된 레벨. 한 점이면 고정 레벨."""
    if len(pts) < 2:
        return 14
    for level in range(16, 5, -1):
        xy = [_mercator(lon, lat, level) for lon, lat in pts]
        xs, ys = [p[0] for p in xy], [p[1] for p in xy]
        if (max(xs) - min(xs)) <= _MAP_W * 0.7 and (max(ys) - min(ys)) <= _MAP_H * 0.7:
            return level
    return 6


def _fetch_static_map(key):
    """(중심lon, 중심lat, level) → data URI. 실패하면 빈 문자열을 캐시(계속 재시도 안 하게)."""
    clon, clat, level = key
    q = {"w": _MAP_W, "h": _MAP_H, "center": f"{clon},{clat}", "level": level,
         "format": "jpg", "scale": _MAP_SCALE, "maptype": _MAP_TYPE}
    url = f"{_STATIC_MAP_URL}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={
        "x-ncp-apigw-api-key-id": _NCP_KEY_ID, "x-ncp-apigw-api-key": _NCP_KEY})
    uri = ""
    try:
        with urllib.request.urlopen(req, timeout=_MAP_TIMEOUT) as r:
            # jpg: 위성 사진은 png 로 받으면 650KB, jpg 는 120KB 다(실측). 전국 뷰와 확대 뷰를
            # 둘 다 HTML 에 실어 보내므로(gateAfterNorm 이 골라 보여준다) 용량 차이가 그대로
            # 폴링 전송량이 된다 → jpg 로 받는다. 위성 사진이라 눈으로는 차이가 없다.
            uri = "data:image/jpeg;base64," + base64.b64encode(r.read()).decode("ascii")
    except Exception as e:
        print(f"[app_ui_live] 지도 로드 실패 (지도 없이 계속): {e}", flush=True)
    with _map_lock:
        _map_cache[key] = uri
        if len(_map_cache) > 8:              # 오래된 것부터 버린다 (민원이 계속 바뀌므로)
            _map_cache.pop(next(iter(_map_cache)))
        _map_pending.discard(key)


def _map_uri(key):
    """캐시에 있으면 반환, 없으면 백그라운드로 받기 시작하고 None (아직 준비 안 됨)."""
    with _map_lock:
        if key in _map_cache:
            return _map_cache[key]
        if key in _map_pending:
            return None
        _map_pending.add(key)
    threading.Thread(target=_fetch_static_map, args=(key,), daemon=True).start()
    return None


def _map_view(clon: float, clat: float, level: int, pins, cls: str) -> str:
    """한 장의 지도 뷰(이미지 + 마커). 이미지를 아직 못 받았으면 자리표시, 실패면 빈 문자열."""
    key = (round(clon, 6), round(clat, 6), level)
    uri = _map_uri(key)
    if uri is None:
        return (f'<div class="mapwrap mapwrap-load {cls}" data-lp>'
                f'<span data-lp>지도 불러오는 중…</span></div>')
    if not uri:
        return ""                              # 지도 못 받으면 조용히 생략(키워드는 그대로 보임)
    cx, cy = _mercator(clon, clat, level)
    marks = []
    for kind, lon, lat, name, label in pins:
        x, y = _mercator(lon, lat, level)
        # 요청 이미지 안에서의 위치 비율(0~1). 표시 크기·여백은 JS(placeMapPins)가 실측해
        # object-fit:contain 으로 letterbox 된 실제 이미지 영역 위에 얹는다 → 왜곡·크롭에도
        # 마커가 정확하다(% 로 박으면 컨테이너 기준이라 letterbox 만큼 어긋난다).
        fx = (_MAP_W / 2 + (x - cx)) / _MAP_W
        fy = (_MAP_H / 2 + (y - cy)) / _MAP_H
        tip = _html.escape(f"{label}: {name}" if name else label)
        marks.append(f'<span class="mpin mpin-{kind}" data-lp title="{tip}" '
                     f'data-fx="{fx:.4f}" data-fy="{fy:.4f}"></span>')
    return (f'<div class="mapwrap {cls}" data-lp><img class="mapimg" data-lp src="{uri}" '
            f'alt="위치 지도">{"".join(marks)}</div>')


# 범례는 지도 '바로 위' 한 줄로 (지도 밖, 오버레이 아님 — 마커와 안 겹친다).
# 마커가 아직 없어도(대기·처리 중) 항상 띄운다 — 지도에 무슨 색이 뭘 뜻하는지 미리 알린다.
# 절대 안 바뀌는 값이라 폴링 대상이 아니다(= 다시 그려지는 일이 없다).
MAP_LEGEND = ('<div class="maplegend" data-lp>'
              '<span class="lg lg-rep" data-lp>신고자 위치</span>'
              '<span class="lg-sep" data-lp>|</span>'
              '<span class="lg lg-odor" data-lp>냄새 위치</span></div>')


def map_idle_html() -> str:
    """대기 화면 지도 = 대한민국 전국 (마커 없음). 민원 내용과 무관한 **고정** 값이라,
    한 번 뜬 뒤에는 폴링이 값이 같다고 보고 건드리지 않는다 → 다시 그려지며 번쩍이지 않는다.
    (예전엔 지도 한 칸에 전국·확대를 같이 담아 보냈는데, 확대본이 준비되는 순간 칸 전체가
     교체되면서 전국 지도 <img> 도 새로 만들어져 대화 중간에 화면이 한 번 번쩍였다.)"""
    return _map_view(*_MAP_DEFAULT_CENTER, _MAP_DEFAULT_LEVEL, [], "mapview-idle")


def map_zoom_html(o: dict) -> str:
    """추출된 좌표(rep_*/odor_*)로 두 점에 맞춰 확대한 지도 + 마커. 좌표가 없으면 빈 문자열.
    이 칸은 CSS 로 숨겨져 있고(.kw-map-zoom), **표준어 변환 대화가 끝나면** JS 가 켜면서
    전국 지도 칸을 끈다 — 확대·마커는 '키워드 추출 결과'와 한 세트라 같이 나와야 한다.
    숨은 칸이라 값이 중간에 채워져도 화면에는 아무 변화가 없다."""
    pins = []
    for kind, lon_k, lat_k, name_k, label in (
            ("rep", "rep_lon", "rep_lat", "rep_name", "신고자 위치"),
            ("odor", "odor_lon", "odor_lat", "odor_name", "냄새 위치")):
        lon, lat = o.get(lon_k), o.get(lat_k)
        if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
            pins.append((kind, float(lon), float(lat), o.get(name_k) or "", label))
    if not pins:
        return ""
    pts = [(p[1], p[2]) for p in pins]
    level = _pick_level(pts)
    clon = sum(p[0] for p in pts) / len(pts)
    clat = sum(p[1] for p in pts) / len(pts)
    return _map_view(clon, clat, level, pins, "mapview-pins")


def stt_stage_html(stream_text: str, job: str, analyzing: bool, chat: str = "") -> str:
    """STT 칸 = [전사 타이핑] → [민원 분석중…] → [말풍선] 3단을 **한 컨테이너에 같이** 담아
    두고, 지금 어느 단을 보여줄지는 JS(typeStt)가 순서대로 넘긴다.

    왜 파이썬이 직접 갈아끼우지 않나:
      · '분석중' 신호가 오는 즉시 로딩화면으로 바꾸면 마지막 청크가 타이핑되기 전에 사라져
        전사문 끝이 잘린다(요청 1).
      · 반대로 JS 완료를 기다리게만 하면 다음 단계(step≥1)가 먼저 도착해 로딩화면을 아예
        건너뛴다(요청 2 의 '안 보인다'). 3단을 다 넘겨두면 어느 쪽도 안 생긴다 —
        타이핑이 끝나야 로딩이 뜨고, 로딩을 최소 시간 보여준 뒤 말풍선으로 넘어간다.

    data-full: 목표 전사문. step≥1 에선 빈 값으로 두고 JS 가 직전까지 받은 값을 그대로 쓴다
    (그 판에 이어서 타이핑을 마무리해야 하므로). data-job 이 바뀌면 새 민원 = 처음부터."""
    loading = ('<div class="stt-loading" data-lp>'
               '<div class="stt-loading-txt" data-lp>민원 분석중'
               '<span class="stt-dots" data-lp><i data-lp>.</i><i data-lp>.</i><i data-lp>.</i></span>'
               '</div></div>')
    return (f'<div class="stt-stage" data-lp data-job="{_html.escape(job)}" '
            f'data-full="{_html.escape(stream_text)}" '
            f'data-analyzing="{"1" if analyzing else "0"}">'
            f'<div class="stt-stream" data-lp><span class="stt-typed" data-lp></span>'
            f'<span class="stt-caret" data-lp></span></div>'
            f'{loading}{chat}</div>')


# 키워드 항목 아이콘 (KW_LABELS 와 같은 순서).
#
# ■ 왜 이모지가 아니라 인라인 SVG 인가
#   ① 이모지는 OS·브라우저마다 다른 그림으로 그려진다 — 시연 화면과 검토자 화면이 달라진다.
#   ② 이모지는 자기 색을 갖고 있어 팔레트(--kw-*)를 못 따르고, 다크 모드에서 특히 튄다.
#   ③ 스크린리더가 이모지 이름을 그대로 읽는다("둥근 압정 신고자 위치").
#   SVG 는 셋 다 없고 currentColor 로 색을 물려받는다. 파일에 직접 박으므로 외부 요청도 없다.
#
# 옆에 라벨 글자가 그대로 보이므로 아이콘은 **장식**이다 → aria-hidden="true" 로
# 접근성 트리에서 빼서 같은 말을 두 번 읽지 않게 한다.
_KW_SVG = (
    # 신고자 위치 — 지도 핀 (지도 마커 .mpin-rep 과 같은 뜻·같은 색)
    '<path d="M12 21.5c4.2-4.4 6.5-7.7 6.5-10.7a6.5 6.5 0 1 0-13 0c0 3 2.3 6.3 6.5 10.7Z"/>'
    '<circle cx="12" cy="10.6" r="2.4"/>',
    # 냄새 종류 — 퍼지는 바람결
    '<path d="M3.6 8.4h8.1a2.9 2.9 0 1 0-2.9-2.9"/>'
    '<path d="M3.6 12.4h11.3a2.9 2.9 0 1 1-2.9 2.9"/>'
    '<path d="M3.6 16.4h5.5"/>',
    # 냄새 강도 — 높아지는 막대
    '<path d="M6.5 18.4v-4.2"/><path d="M12 18.4v-8.3"/><path d="M17.5 18.4v-12.5"/>',
    # 냄새 주기 — 시계
    '<circle cx="12" cy="12" r="8.2"/><path d="M12 7.3v5l3.4 2"/>',
    # 냄새 위치 — 조준점(추정 지점). 지도 마커 .mpin-odor 와 같은 색으로 묶는다
    '<circle cx="12" cy="12" r="7.4"/><circle cx="12" cy="12" r="2.2"/>'
    '<path d="M12 2.7v2.1M12 19.2v2.1M2.7 12h2.1M19.2 12h2.1"/>',
)


def kw_html(label: str, value: str, idx: int = 0) -> str:
    v = (value or "").strip()
    dim = "" if v and v != "미언급" else " dim"
    ico = (f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
           f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
           f'focusable="false">{_KW_SVG[idx % len(_KW_SVG)]}</svg>')
    return (f'<div class="kw kw-i{idx}{dim}">'
            f'<div class="kw-head">'
            f'<span class="kw-ico">{ico}</span>'
            f'<span class="kw-label">{label}</span></div>'
            f'<div class="kw-value{dim}">{_html.escape(v) if v else "—"}</div></div>')


_EMPTY = {
    "file": "", "stt": "", "stt_stream": "", "stt_analyzing": False,
    "subregion": "", "region": "", "places": "",
    "search_vec": "", "search_ngram": "", "search_jamo": "",
    "matches": "", "rag": "", "corrected": "", "corrected_nor": "", "normalized": "",
    "keywords": "", "kw_pred": [""] * 5,
    # 지도 마커 좌표 (서버가 지오코딩 후 채운다. 못 찾으면 None)
    "rep_lon": None, "rep_lat": None, "rep_name": "",
    "odor_lon": None, "odor_lat": None, "odor_name": "",
}

# '직전에 보낸 값'은 모듈 전역으로 두면 안 된다 → gr.State 로 세션(브라우저 탭)별로 갖는다.
# 전역이면 두 번째 탭이나 새로고침한 페이지가 "값이 안 바뀌었다"는 판정을 받아 gr.skip()
# 만 돌려받고, 다음 변화가 올 때까지 **빈 화면으로 남는다**.
# 새 민원 감지 + 완료 감지 상태. primed: 최소 1회 폴링해서 '이미 있던 작업'과 '새 작업'을 구분
_TOAST_NEW = '<div class="toast-notice">🔔 새로운 민원이 업로드되었습니다</div>'
_TOAST_ERR = '<div class="toast-notice err">⚠️ 처리 중 오류가 발생했습니다</div>'


def _toast_done(job: str) -> str:
    """완료 알림. data-gate="norm" 이 붙어 있으면 JS 가 이걸 바로 띄우지 않고 숨겨 두었다가
    **표준어 변환 대화 타이핑이 끝난 뒤** 띄운다 — 대화가 아직 흐르는 중에 '처리 완료'가
    먼저 뜨면 앞뒤가 안 맞는다(요청 4). data-job 은 같은 민원에 두 번 띄우지 않기 위한 표식."""
    return ('<div class="toast-notice done" data-gate="norm" '
            f'data-job="{_html.escape(job or "")}">✅ 처리가 완료되었습니다</div>')
_DONE_STEP = len(STEPS)  # 모든 단계 완료 시 step 값
# t0/elapsed: 경과 시간. job 토큰이 바뀐 시점부터 재고, 완료·오류에서 멈춘다.
# 이 UI 프로세스가 재는 값이므로 **업로드 전부터 UI 가 떠 있어야** 정확하다(항상 그렇다).
# 이미 진행 중인 작업을 뒤늦게 보기 시작한 경우엔 t0 를 잡지 않아 표시하지 않는다.
_JOB = {"token": None, "primed": False, "ttl": 0, "html": "", "done_shown": True,
        "t0": None, "elapsed": None}


def poll(prev):
    """0.1초마다 호출. 공유 파일(live_progress.json)을 읽어 값이 바뀐 컴포넌트만 갱신.
    prev: 이 세션에 직전 보낸 값 (gr.State). 갱신된 사본을 첫 반환값으로 되돌려준다."""
    prog = read_progress()
    if not prog:
        step, error, o = -1, None, _EMPTY
    else:
        step = prog.get("step", -1)
        error = prog.get("error")
        o = {**_EMPTY, **(prog.get("outputs") or {})}

    # 토스트: job 토큰이 바뀌면 '새 민원', step이 마지막에 도달하면 '완료'를 30틱(약 3초) 띄운다.
    # (UI를 처음 켰을 때 '이미 진행/완료된 작업'에는 알림을 띄우지 않도록 primed로 구분)
    job = prog.get("job") if prog else None
    primed = _JOB["primed"]
    if job and job != _JOB["token"]:
        if primed and step < _DONE_STEP:
            _JOB["ttl"], _JOB["html"] = 30, _TOAST_NEW
        _JOB["token"] = job
        _JOB["done_shown"] = step >= _DONE_STEP  # 이미 끝난 상태로 처음 본 작업은 완료알림 생략
        # 새 작업 → 시계 재시작. 이미 끝나 있던 걸 처음 본 경우는 걸린 시간을 알 수 없다.
        _JOB["t0"] = None if step >= _DONE_STEP else time.monotonic()
        _JOB["elapsed"] = None
    if primed and job and step >= _DONE_STEP and not _JOB["done_shown"]:
        _JOB["ttl"], _JOB["html"] = 30, (_TOAST_ERR if error else _toast_done(job))
        _JOB["done_shown"] = True
    _JOB["primed"] = True

    if _JOB["t0"] is not None:
        _JOB["elapsed"] = time.monotonic() - _JOB["t0"]
        if step >= _DONE_STEP or error is not None:
            _JOB["t0"] = None      # 완료·오류에서 시계를 멈춰 마지막 값을 유지한다

    ttl = _JOB["ttl"]
    notice_html = _JOB["html"] if ttl > 0 else ""
    _JOB["ttl"] = max(0, ttl - 1)

    kw = (o.get("kw_pred") or [""] * 5) + [""] * 5
    # 보정 칸은 체크 모드마다 표식이 다른 두 판본이 온다. 둘 다 넣어 두고 CSS(.mode-*)가
    # 켜진 쪽만 보여준다 — 한 벌에 표식을 섞으면 붙어 있는 변경 단어의 박스가 끊긴다
    # (사유는 lifespan._disp 주석). 표준어가 아직 없으면 nor 판본은 보정 원본으로 대체.
    # '하나씩 띄우기' 표식은 **칸마다 따로** 단다 — 그 칸의 단계가 끝난 순간에.
    # (전체 완료를 기다리면, 각 칸은 자기 결과가 나오는 순간 한꺼번에 다 떠 버리고
    #  맨 끝에 가서야 세 칸이 동시에 다시 재생돼 이상해진다.)
    # SECTIONS 의 hi 를 그대로 쓰므로 단계 번호가 바뀌어도 같이 따라간다:
    #   STT 0..0 → step>0(=1, remove_noise) 에 확정 / 보정 1..5 → step>5(=6, correct_remaining)
    #   표준어 6..6 → step>6(=7, normalize_dialect). 즉 '내용이 처음 뜨는 순간'과 정확히 같다.
    _sec_done = [step > hi and error is None for (_lbl, _lo, hi) in SECTIONS]
    cor_cor = chat_html(o["corrected"], job or "", _sec_done[1])
    cor_nor = chat_html(o["corrected_nor"] or o["corrected"], job or "", _sec_done[1])
    # STT 칸은 순서대로 세 단을 지난다:
    #   1) 전사 진행 중(step 0)      → stt_stream 누적 전사문을 글자 단위로 타이핑
    #   2) 전사 완료·화자분리 중      → "민원 분석중…" 로딩
    #   3) step≥1                    → 상담원/민원인 말풍선
    # 어느 단을 보여줄지는 JS 가 정하므로(사유: stt_stage_html 주석) 세 단을 다 넘긴다.
    stt_stream = o.get("stt_stream") or ""
    analyzing = bool(o.get("stt_analyzing"))
    stt_chat = chat_html(o["stt"], job or "", _sec_done[0])
    if error is not None:
        stt_col = stt_chat
    elif step == 0 and (stt_stream or analyzing):
        stt_col = stt_stage_html(stt_stream, job or "", analyzing)
    elif stt_chat:
        # data-full 은 빈 값 → JS 가 직전까지 타이핑하던 전사문을 이어서 마무리한다.
        # (페이지를 방금 열어 타이핑한 적이 없으면 곧바로 말풍선으로 넘어간다)
        stt_col = stt_stage_html("", job or "", True, stt_chat)
    else:
        stt_col = ""
    texts = [
        stt_col,
        (f'<div class="cview cview-cor" data-lp>{cor_cor}</div>'
         f'<div class="cview cview-nor" data-lp>{cor_nor}</div>') if cor_cor else "",
        chat_html(o["normalized"], job or "", _sec_done[2]),
    ]

    snapshot = {
        "notice": notice_html,
        "progress": progress_html(step, error, o.get("file", ""), _JOB["elapsed"]),
    }
    for i, ph in enumerate(PLACEHOLDERS):
        snapshot[f"text_{i}"] = texts[i] or f'<div class="placeholder" data-lp>{ph}</div>'
    snapshot["kw_map"] = map_idle_html()      # 고정 — 한 번 뜨면 그대로
    snapshot["kw_zoom"] = map_zoom_html(o)    # 숨은 칸. 대화가 끝나면 JS 가 켠다
    for i, label in enumerate(KW_LABELS):
        snapshot[f"kw_{i}"] = kw_html(label, kw[i], i)
    for i in range(len(SECTIONS)):
        snapshot[f"section_{i}"] = section_block(i, step, error)

    sent = dict(prev or {})
    results = []
    for key, value in snapshot.items():
        if sent.get(key) == value:
            results.append(gr.skip())
        else:
            sent[key] = value
            results.append(value)
    return (sent, *results)


# ── 한 화면에 다 들어오게 (세로 맞춤) ──────────────────────────────
# 여기서 여러 번 틀렸으므로 지금 방식과, 하지 않는 것들을 적어둔다.
#
# ■ 하지 않는 것 ① 스크롤 잠금 (overflow: hidden)
#   "화면 고정"을 위해 걸었다가 온갖 증상을 만들었다. hidden 은 현재 스크롤 위치를
#   0 으로 돌려주지 않으므로, 내려간 상태에서 걸면 위가 잘린 채 얼어붙고 스크롤로도
#   못 올라간다. 반대로 풀 때 위로 되돌리면 아래로 내려갈 수가 없다.
#   **애초에 필요가 없다** — 다 들어오면 스크롤할 게 없어서 화면은 저절로 고정된다.
#   잠금은 "안 들어왔을 때 그 사실을 숨기는" 역할만 했다.
#
# ■ 하지 않는 것 ② 위치(getBoundingClientRect) 재기
#   화면 기준 값이라 스크롤된 만큼 어긋난다. 보정하려면 '누가 스크롤 주체인가'를
#   알아야 하는데 Gradio 내부 어느 div 인지 확실치 않고, 못 맞히면 조용히 0 이 된다.
#
# ■ 하지 않는 것 ③ CSS flex 사슬 / 문서 전체 높이(scrollHeight)
#   flex 사슬은 Gradio 내부 스타일에 막히고, scrollHeight 는 컨테이너가 자체 스크롤하면
#   창 높이에서 더 커지지 않아 넘침을 감지하지 못한다.
#
# ■ 지금 방식: 높이(offsetHeight)만 더한다
#   offsetHeight 는 레이아웃 높이라 스크롤 위치와 원리적으로 무관하다.
#     텍스트창높이 = 창높이 - (제목카드 + 키워드카드 + 카드테두리·칩 등) - 여백
#   전부 실측이고 위치는 안 쓴다. 0.4 초마다 확인하되 2px 넘게 어긋날 때만 손댄다.
#   계산이 어긋나도 최악의 결과는 "페이지가 조금 스크롤된다" 뿐이다 — 잘리지 않는다.
_FIT_BODY = """
  // GAP=0 이어야 안전하다. 여백을 빼면, 컨테이너가 어떤 이유로든 화면 높이에 고정돼
  // offsetHeight 가 안 변하는 순간 h = 박스 - GAP 이 되어 매 틱 GAP 만큼 영원히 줄어든다.
  // 아래 여백은 컨테이너 하단 패딩(16px)이 이미 준다.
  // DEAD: 이만큼 어긋나야 손댄다. 1~2px 반올림 차이가 쌓여 드리프트가 되는 것을 막는다.
  const MIN = 160, GAP = 0, DEAD = 6;

  // .diffbox 는 Gradio 블록과 그 내부 .prose 양쪽에 붙는다. 높이를 정하고 스크롤하는
  // 주체는 '중첩되지 않은 바깥쪽'뿐이므로 그것만 고른다 (둘 다 건드리면 스크롤러가
  // 중첩되어 마지막 말풍선에 닿지 못한다).
  function outerBoxes() {
    return [...document.querySelectorAll('.diffbox')]
      .filter(e => !e.parentElement || !e.parentElement.closest('.diffbox'));
  }

  function fit() {
    const box  = outerBoxes()[0];
    const cont = document.querySelector('.gradio-container');
    if (!box || !cont) return;

    // 텍스트창을 뺀 '나머지 전부'를 한 번에 실측한다. 항목을 세어 더하면 반드시 빠뜨린다
    // — 실제로 .main.app 패딩(32) · 자식 gap(48) · Gradio API 푸터(37) · 알림 홀더(20)를
    // 놓쳐서 100px 이 넘쳤다. 컨테이너는 height:auto 라 offsetHeight 가 내용 전체 높이이고,
    // 우리가 크기를 바꾸는 건 텍스트창뿐이므로 이 차이가 곧 '나머지'다.
    const overhead = cont.offsetHeight - box.offsetHeight;
    const h = Math.max(MIN, Math.round(window.innerHeight - overhead - GAP));
    if (Math.abs(h - box.offsetHeight) <= DEAD) return;
    // CSS 쪽 height 가 !important 라 인라인 style 로는 못 이긴다 → 같은 우선순위로 준다.
    // 키워드 인셋(.kw-list)도 같은 높이를 준다 — 네 칸의 위/아래 선이 정확히 맞고,
    // 이 칸이 제멋대로 늘어나 행 높이를 붙잡는 일도 없어진다(위 CSS 주석 참고).
    [...outerBoxes(), ...document.querySelectorAll('.kw-list')]
      .forEach(b => { b.style.setProperty('height', h + 'px', 'important'); });
  }
  console.log('[app_ui_live] 세로 자동맞춤 ON');
  document.documentElement.dataset.fit = 'on';
  fit();
  window.addEventListener('resize', fit);
  setInterval(fit, 400);
"""

# 대화 박스 스크롤 위치 유지. 세로 자동맞춤과 독립이라 UI_FIT=0 일 때도 이건 쓴다.
_KEEP_SCROLL_BODY = """
  // 폴링이 대화 HTML 을 통째로 갈아끼우면 scrollTop 이 0 으로 튄다. 그래서 교체 직후
  // 위치를 다시 잡아주는데, '어디로' 가 두 가지다:
  //   · 새 민원이면(data-job 이 바뀌면) → 맨 위. 대화는 상담원 첫 마디부터 읽는 게 맞고,
  //     바닥에 붙여두면 그 첫 마디가 스크롤 밖으로 나가 안 보인다.
  //   · 같은 민원의 갱신이면 → 사용자가 두고 본 위치를 지킨다. 단계마다 위로 튀면
  //     읽는 걸 방해한다.
  // 되돌리는 중의 scroll 이벤트는 무시한다(안 그러면 방금 우리가 옮긴 위치가 사용자
  // 의도로 기록된다).
  function keepScroll(box) {
    let keep = 0, job = null, restoring = false;
    box.addEventListener('scroll', () => { if (!restoring) keep = box.scrollTop; });
    if (!window.MutationObserver) return;
    new MutationObserver(() => {
      const chat = box.querySelector('.chat');
      const j = chat ? chat.dataset.job : null;
      if (j !== job) { job = j; keep = 0; }
      // 타이핑 중에는 타이핑 쪽 자동 스크롤(맨 아래 따라가기)이 이긴다. 여기서 되돌리면
      // 글자마다 위치를 서로 잡아당겨 화면이 떨린다(타이핑은 characterData 도 바꾼다).
      if (box.dataset.typing === '1') { keep = box.scrollTop; return; }
      if (box.scrollTop === keep) return;
      restoring = true;
      box.scrollTop = keep;
      restoring = false;
    }).observe(box, { childList: true, subtree: true, characterData: true });
  }
  // 스크롤 유지도 바깥 박스에만 (안쪽은 스크롤하지 않는다)
  [...document.querySelectorAll('.diffbox')]
    .filter(e => !e.parentElement || !e.parentElement.closest('.diffbox'))
    .forEach(keepScroll);

  // 실행 중 표시를 칩이 아니라 '바깥 카드(.pane)' 로 옮긴다: 그 카드가 '진행 중'이면
  // .pane-running 을 붙여 카드 전체가 반짝이게 한다.
  //
  // ★ 기준은 서버 단계가 아니라 **화면에 보이는 진행**이다. 서버 단계는 타이핑보다 앞서가서,
  //   그냥 칩의 running 만 따라가면 아직 대화가 흘러나오는 중인 칸의 반짝임이 먼저 꺼지고
  //   다음 칸이 반짝인다. 그래서
  //     · 타이핑이 남은 칸(pend)      → 서버가 '완료'라 해도 계속 진행 중으로 보여준다
  //     · 그보다 뒤 칸               → 서버가 '진행 중'이라 해도 대기로 되돌린다(.pane-hold)
  //   pend 는 typeChats/typeStt 가 매 틱 알려준다.
  function syncPanes(pend) {
    const panes = [...document.querySelectorAll('.pane')];
    let cur = Infinity;                       // 지금 화면이 보여주고 있는 칸
    panes.forEach((p, i) => { if (pend[i]) cur = Math.min(cur, i); });
    panes.forEach((pane, i) => {
      const chip = pane.querySelector('.section-chip');
      const srv = !!(chip && chip.classList.contains('running'));
      // 지금 실제로 나오고 있는 칸은 pend 중 가장 앞 칸 하나뿐이다. 그보다 뒤 칸은 결과가
      // 도착해 있어도 화면에는 아직 안 나온 상태이므로 (서버가 뭐라 하든) 대기로 보여준다.
      // 키워드 칸도 마찬가지 — 값은 표준어 대화가 끝날 때까지 감춰 두므로 칩만 완료면 어긋난다.
      const typing = i === cur;
      const hold = i > cur;
      pane.classList.toggle('pane-typing', typing);
      pane.classList.toggle('pane-hold', hold);
      pane.classList.toggle('pane-running', typing || (srv && !hold));
    });
  }

  // 지도 마커를 object-fit:contain 으로 letterbox 된 '실제 이미지 영역' 위에 얹는다.
  // 서버는 마커의 이미지 내 위치 비율(data-fx/fy)만 주고, 표시 크기·여백은 여기서 실측한다
  // → 지도 영역 비율이 이미지와 달라도(위아래/좌우 여백이 생겨도) 마커가 안 어긋난다.
  function placeMapPins() {
    document.querySelectorAll('.mapwrap').forEach((wrap) => {
      const img = wrap.querySelector('.mapimg');
      const pins = wrap.querySelectorAll('.mpin');
      if (!img || !img.naturalWidth || !pins.length) return;
      const bw = wrap.clientWidth, bh = wrap.clientHeight;
      if (!bw || !bh) return;      // 아직 숨겨진 뷰(확대 지도 대기 중) — 재면 전부 0 이 된다
      const scale = Math.min(bw / img.naturalWidth, bh / img.naturalHeight);
      const dispW = img.naturalWidth * scale, dispH = img.naturalHeight * scale;
      const offX = (bw - dispW) / 2, offY = (bh - dispH) / 2;
      pins.forEach((p) => {
        const fx = parseFloat(p.dataset.fx), fy = parseFloat(p.dataset.fy);
        if (isNaN(fx) || isNaN(fy)) return;
        p.style.left = (offX + fx * dispW).toFixed(1) + 'px';
        p.style.top = (offY + fy * dispH).toFixed(1) + 'px';
        p.style.visibility = 'visible';
      });
    });
  }
  placeMapPins();
  setInterval(placeMapPins, 250);          // 폴링이 지도 HTML 을 갈아끼우거나 크기가 변해도 따라간다
  window.addEventListener('resize', placeMapPins);

  // ── 타이핑 애니메이션 (전사문·대화 공통) ──────────────────────────────
  // 한 틱(TICK ms)에 RATE 글자. STT 전사 타이핑과 대화 말풍선 타이핑이 **같은 속도**를 쓴다.
  const TICK = 55, RATE = 2;

  // STT 칸은 [전사 타이핑] → [민원 분석중…] → [말풍선] 세 단을 지난다. 서버는 세 단을 한
  // 컨테이너(.stt-stage)에 다 담아 보내고, 지금 어느 단인지는 여기서 정한다.
  //   · 칠 글자가 남아 있으면 무슨 일이 있어도 전사문 단에 머문다 → 마지막 청크가 안 잘린다.
  //   · 로딩 단은 최소 LOAD_MIN 만큼 유지한다 → 다음 단계가 빨리 와도 반드시 눈에 보인다.
  // 얼마나 노출했는지(shown)와 목표 전사문(full)은 DOM 이 아니라 여기 둔다 — 폴링이
  // innerHTML 을 통째로 갈아끼우므로 요소에 남긴 진행도는 날아간다.
  const LOAD_MIN = 1400;
  const stt = { job: null, full: '', shown: 0, loadAt: 0 };
  // advance=false 면 진행은 그대로 두고 '지금 상태만 다시 그린다'. 폴링이 HTML 을 갈아끼운
  // 직후에 이걸 불러 깜빡임을 없앤다 — 진행까지 같이 올리면 타이핑이 빨라져 버린다.
  // 반환: STT 칸이 아직 '내보내는 중'인가 (전사 타이핑 중이거나 분석중 로딩 단).
  function typeStt(advance) {
    const st = document.querySelector('.stt-stage');
    if (!st) { stt.job = null; stt.full = ''; stt.shown = 0; stt.loadAt = 0; return false; }
    const job = st.dataset.job || '';
    if (stt.job !== job) { stt.job = job; stt.full = ''; stt.shown = 0; stt.loadAt = 0; }
    // 청크가 붙으면 목표가 길어진다. step≥1 에선 서버가 빈 값을 주므로 직전 목표를 유지한다.
    const full = st.dataset.full || '';
    if (full.length > stt.full.length) stt.full = full;
    const analyzing = st.dataset.analyzing === '1';
    const stream = st.querySelector('.stt-stream');
    const load = st.querySelector('.stt-loading');
    const chat = st.querySelector('.chat');

    if (advance && stt.shown < stt.full.length) {
      stt.shown = Math.min(stt.full.length, stt.shown + RATE);
    }
    const typed = stream ? stream.querySelector('.stt-typed') : null;
    const s = stt.full.slice(0, stt.shown);
    const box = st.closest('.diffbox');
    // 전사 타이핑 중 표시(스크롤 위치 유지 로직이 비켜주게). 끝나면 말풍선 타이핑 쪽이 갱신한다.
    if (box && stt.shown < stt.full.length) box.dataset.typing = '1';
    if (typed && typed.textContent !== s) {
      typed.textContent = s;
      if (box) box.scrollTop = box.scrollHeight;  // 새 글자가 아래에 붙으므로 맨 아래를 따라간다
    }

    const typing = stt.shown < stt.full.length;
    // 한 글자도 타이핑한 적이 없다면(결과가 다 나온 뒤에 페이지를 열었다) 로딩은 건너뛴다.
    const neverTyped = stt.full.length === 0;
    let phase;
    if (typing || (!analyzing && !chat)) phase = 'type';
    else if (neverTyped && chat) phase = 'chat';
    else {
      if (!stt.loadAt) stt.loadAt = performance.now();
      phase = (chat && performance.now() - stt.loadAt >= LOAD_MIN) ? 'chat' : 'load';
    }
    if (stream) stream.style.display = phase === 'type' ? 'block' : 'none';
    if (load) load.style.display = phase === 'load' ? 'flex' : 'none';
    if (chat) chat.style.display = phase === 'chat' ? 'flex' : 'none';
    return phase === 'load' || (phase === 'type' && typing);
  }

  // 각 칸의 단계가 끝나 결과가 확정되면(.chat[data-done="1"]) 그 대화를 챗봇처럼 타이핑해
  // 보여준다. 말풍선 사이에 쉼은 없다 — 한 말풍선이 끝나면 바로 다음 말풍선이 이어진다.
  // 진행도는 DOM 이 아니라 여기(칸:판본 별)에 둔다. 폴링이 HTML 을 갈아끼워도 이어서 친다
  // (STT 칸은 보정이 나오면 변경 태그가 붙어 다시 그려지고, 보정 칸은 표준어가 나오면
  //  '표준어 체크용 판본'이 붙어 다시 그려진다).
  const twState = {};
  // 보정 칸의 두 판본(cor/nor)은 표식만 다르고 글자 수가 같다. 판본을 바꿔 보여줄 때
  // 진행도를 그대로 옮겨 준다 — 안 그러면 새로 보이게 된 판본이 shown=0 에서 시작해
  // 이미 다 나온 대화를 처음부터 다시 친다.
  function twMirror(toNor) {
    const from = toNor ? 'cor' : 'nor', to = toNor ? 'nor' : 'cor';
    Object.keys(twState).forEach((k) => {
      const p = k.split(':');
      if (p[1] !== from) return;
      const s = twState[k];
      twState[p[0] + ':' + to] = {job: s.job, shown: s.shown};
    });
  }
  function twKey(chat) {
    const panes = [...document.querySelectorAll('.pane')];
    const view = chat.closest('.cview');
    return panes.indexOf(chat.closest('.pane')) + ':' +
      (view ? (view.classList.contains('cview-nor') ? 'nor' : 'cor') : '');
  }
  // 말풍선의 텍스트 노드 목록을 요소에 캐시한다. 변경 강조 span(마크업)을 살린 채 '앞 k 글자'
  // 만 보이게 하려면 노드별 원본 문자열이 필요하다.
  //
  // ★ 캐시를 언제 버리는지가 중요하다. Gradio 는 새 HTML 을 통째로 꽂는 게 아니라 **기존
  //   DOM 을 살려 두고 고쳐 넣는다**(실측). 그래서 사투리 보정이 끝나 STT 칸이 '변경 태그가
  //   붙은 판본'으로 바뀌어도 .turn/.bubble 과 맨 앞 텍스트 노드는 같은 객체로 남는다 →
  //   요소가 그대로라는 이유로 캐시를 재사용하면, 캐시에 든 옛 문자열(그 말풍선 전문)을
  //   twRender 가 첫 노드에 다시 써 버려 '전문 + 새 판본' 이 이어붙은 두 배 텍스트가 된다.
  //   → 우리가 마지막으로 그린 결과(__sig)와 현재 innerHTML 을 비교해, 남이 손댔으면 버린다.
  function twSig(turn) {
    const b = turn.querySelector('.bubble');
    if (b) turn.__sig = b.innerHTML;
  }
  function twPrep(turn) {
    const b = turn.querySelector('.bubble');
    if (turn.__tw && b && b.innerHTML !== turn.__sig) {
      turn.__tw = null;                 // 폴링이 내용을 갈아끼웠다 → 다시 잰다
      b.__sized = 0;                    // 크기 예약도 새 내용 기준으로 다시 (지금 DOM 은 전문)
      b.style.minWidth = ''; b.style.minHeight = '';
    }
    if (turn.__tw) return turn.__tw;
    const nodes = [];
    if (b) {
      const it = document.createTreeWalker(b, NodeFilter.SHOW_TEXT);
      for (let n = it.nextNode(); n; n = it.nextNode()) nodes.push({node: n, text: n.nodeValue});
    }
    let len = 0;
    nodes.forEach((e) => { len += e.text.length; });
    turn.__tw = {nodes: nodes, len: len};
    twSig(turn);
    return turn.__tw;
  }
  function twRender(turn, k) {
    twPrep(turn).nodes.forEach((e) => {
      const n = Math.max(0, Math.min(e.text.length, k));
      k -= e.text.length;
      const cut = e.text.slice(0, n);
      if (e.node.nodeValue !== cut) e.node.nodeValue = cut;
      // 아직 한 글자도 안 나온 강조 span 은 빈 배지로 남아 얼룩처럼 보인다 → 접어 둔다.
      const p = e.node.parentElement;
      if (p && p.hasAttribute('data-diff')) p.style.display = n > 0 ? 'inline' : 'none';
    });
    twSig(turn);                        // 여기까지가 '우리가 그린 상태' — 이후 차이는 남의 변경
  }
  // 칸(pane) 별로 '보이는 판본이 아직 타이핑 중인가'. 뒤 칸이 앞 칸을 기다리는 데 쓴다.
  // 숨은 판본(변경표시용 .cview-nor 등)은 세지 않는다 — 그 판본은 나중에 따로 나타나
  // 처음부터 다시 치므로, 세면 뒤 칸이 이유 없이 한 번 더 기다리게 된다.
  // 반환: 칸 번호 → '그 칸이 아직 다 안 나왔나'. 진행 표시(syncPanes)와 결과 게이트가 쓴다.
  // seed: 이미 알고 있는 대기 상태 (STT 칸의 전사 타이핑·분석중).
  function typeChats(advance, seed) {
    const pend = Object.assign({}, seed);
    // 칸 순서대로 처리한다 — 뒤 칸이 앞 칸의 진행 상태를 보고 시작을 미룰 수 있게.
    const chats = [...document.querySelectorAll('.chat[data-done="1"]')]
      .map((c) => ({c: c, key: twKey(c)}))
      .sort((a, b) => parseInt(a.key, 10) - parseInt(b.key, 10));
    chats.forEach(({c: chat, key}) => {
      // STT 칸은 로딩 단이 끝나기 전엔 typeStt 가 감춰 둔다 → 그동안은 타이핑을 시작하지 않는다.
      if (chat.style.display === 'none') return;
      const pane = parseInt(key, 10);
      const job = chat.dataset.job || '';
      const turns = [...chat.children];
      if (!turns.length) return;
      let st = twState[key];
      if (!st || st.job !== job) st = twState[key] = {job: job, shown: 0};
      const vis = chat.offsetParent !== null;   // 숨은 판본은 대기 판정에서 뺀다
      // ★ 각 칸은 **앞 칸 대화가 다 나온 뒤에** 시작한다 (STT → 보정 → 표준어).
      //   그 전에는 한 글자도 내보내지 않고 말풍선을 접어 둔 채 기다린다.
      if (pane >= 1 && pend[pane - 1]) {
        turns.forEach((t) => { if (t.style.display !== 'none') t.style.display = 'none'; });
        if (vis) pend[pane] = true;
        return;
      }
      let total = 0;
      turns.forEach((t) => { total += twPrep(t).len; });
      // 보통은 RATE(=STT 와 같은 속도) 그대로. 대화가 아주 길 때만 20초 안에 끝나게 올린다.
      const rate = Math.max(RATE, Math.ceil(total / (20000 / TICK)));
      const before = st.shown;
      st.total = total;                       // 앞 칸 박스가 이 진행률을 따라간다(liveSync)
      if (advance && st.shown < total) st.shown = Math.min(total, st.shown + rate);
      let left = st.shown;
      turns.forEach((t) => {
        const len = twPrep(t).len;
        if (left > 0 || len === 0) {
          // ★ 말풍선을 켜는 순간 **최종 크기를 재서 min-width/height 로 박아 둔다.**
          //   민원인 말풍선은 오른쪽 정렬(align-items:flex-end) 때문에 내용만큼만 넓어져,
          //   그냥 타이핑하면 박스가 글자에 따라 커진다. 이 시점의 DOM 은 아직 원문 그대로라
          //   (잘라내기는 바로 아래 twRender 에서 한다) 여기서 잰 크기가 곧 최종 크기다.
          //   → 박스가 먼저 제 크기로 뜨고 글자가 안을 채운다. 다 치면 아래에서 풀어 준다.
          if (t.style.display !== 'flex') t.style.display = 'flex';
          const b = t.querySelector('.bubble');
          if (b && !b.__sized) {
            b.__sized = 1;
            b.style.minWidth = b.offsetWidth + 'px';
            b.style.minHeight = b.offsetHeight + 'px';
          }
          twRender(t, left);
          if (b && left >= len && b.style.minWidth) {     // 다 친 말풍선은 예약 해제
            b.style.minWidth = ''; b.style.minHeight = '';
          }
        } else if (t.style.display !== 'none') {
          t.style.display = 'none';
        }
        left -= len;
      });
      // 타이핑 중이라고 표시해 둔다 → 스크롤 위치 유지 로직(_KEEP_SCROLL_BODY)이 비켜준다.
      const box = chat.closest('.diffbox');
      if (box) {
        box.dataset.typing = st.shown < total ? '1' : '0';
        if (st.shown !== before) box.scrollTop = box.scrollHeight;
      }
      if (vis && st.shown < total) pend[pane] = true;
    });
    return pend;
  }

  // 표준어 변환 대화가 다 나오기 전에는 '추출 결과'에 속하는 것들을 내보내지 않는다:
  // 키워드 5칸 · 지도 확대/마커 · 완료 알림. (대화가 아직 흐르는 중에 결과가 먼저 뜨면
  // 앞뒤가 안 맞는다.) 지도는 서버가 전국 뷰와 확대 뷰를 둘 다 보내주므로 여기서 고르면 된다.
  const toastShown = {};
  function gateAfterNorm(pending) {
    document.querySelectorAll('.kw-kwbox .kw').forEach((e) => {
      // visibility 로 감춘다 — display 로 접으면 지도 높이가 들썩인다.
      e.style.visibility = pending ? 'hidden' : 'visible';
    });
    // 지도: 확대·마커 칸을 켤지 말지. 두 칸(전국/확대)을 컴포넌트로 나눠 뒀으므로 여기선
    // 보이기만 바꾼다 — 폴링이 채우는 건 숨은 확대 칸뿐이라 화면이 중간에 다시 그려지지 않는다.
    // 'important' 로 박아야 한다 — .kw-map-slot 안의 칸은 CSS 가 display:flex !important 로
    // 세워두기 때문에 평범한 인라인 style 로는 안 눌린다(실측).
    const zoom = document.querySelector('.kw-map-zoom');
    const idle = document.querySelector('.kw-map-idle');
    const showZoom = !!(zoom && zoom.querySelector('.mapwrap')) && !pending;
    if (zoom) zoom.style.setProperty('display', showZoom ? 'flex' : 'none', 'important');
    if (idle) idle.style.setProperty('display', showZoom ? 'none' : 'flex', 'important');
    if (showZoom) placeMapPins();        // 켜지는 즉시 마커를 얹는다(다음 주기까지 기다리지 않게)
    const t = document.querySelector('.toast-notice[data-gate="norm"]');
    if (!t) return;
    t.style.display = 'none';                       // 서버가 꽂은 원본은 계속 숨겨 둔다
    const j = t.dataset.job || '';
    if (pending || toastShown[j]) return;
    toastShown[j] = true;                           // 같은 민원엔 한 번만
    const c = t.cloneNode(true);
    c.removeAttribute('data-gate');
    c.style.display = '';
    document.body.appendChild(c);
    setTimeout(() => c.remove(), 3200);
  }

  // ── 실시간 변경 태그 ──────────────────────────────────────────────
  // 말풍선이 타이핑되는 동안, 그 전환에서 바뀐 단어에 박스를 걸어 둔다(CSS .live-*).
  // 어느 전환인지는 '지금 실제로 글자가 나오는 칸이 어디인가'로 정한다:
  //   보정 칸(1)이 나오는 중  → live-cor : STT 칸과 보정 칸에 함께 박스
  //   표준어 칸(2)이 나오는 중 → live-nor : 보정 칸과 표준어 칸에 함께 박스
  // 표준어 차례에는 보정 칸을 'b 표식 판본'(.cview-nor)으로 바꿔야 같은 기준의 박스가
  // 양쪽에 걸린다 — 그 전환은 _MODE_BODY 의 applyViews 가 live-nor 를 보고 해 준다.
  //
  // 한 번 켜진 박스는 **거두지 않는다**. 다음 민원의 전사가 시작될 때 함께 지워진다.
  //
  // ★ 상태를 애니메이션 끝(animationend/transitionend)에 걸지 않는다. 타이핑 중에는
  //   폴링이 HTML 을 갈아끼워 진행 중인 애니메이션이 통째로 사라질 수 있어서, 끝 이벤트가
  //   영영 안 오는 경우가 생긴다 — 그 이벤트에 상태를 걸면 거기서 멈춰 버린다.
  //   클래스는 매 틱 직접 확정하고, 애니메이션은 보여주기 전용으로만 쓴다.
  const liveSt = {mode: null, manual: false};
  function liveApply(c) {
    c.classList.toggle('live-cor', liveSt.mode === 'cor');
    c.classList.toggle('live-nor', liveSt.mode === 'nor');
    // 판본 전환(보정 칸 cor↔nor)을 250ms 주기까지 기다리지 않고 그 자리에서 반영한다 —
    // 표준어가 나오기 시작하는 순간에 보정 칸이 잠깐 옛 판본으로 남아 있으면
    // 두 칸의 박스가 어긋나 보인다. (applyViews 는 _MODE_BODY 에 있고 같은 스코프다)
    if (typeof applyViews === 'function') applyViews();
  }
  // 한 칸에서 '지금 화면에 보이는' 대화. 보정 칸엔 두 판본이 늘 같이 들어 있으므로
  // 보이는 쪽만 골라야 한다. 보임 판정은 칸마다 한 번만(span 마다 offsetParent 를 읽으면
  // 매 틱 레이아웃을 여러 번 강제한다).
  function lvChat(pane) {
    const p = document.querySelectorAll('.pane')[pane];
    if (!p) return null;
    return [...p.querySelectorAll('.chat')].find((c) => c.offsetParent !== null) || null;
  }
  // 앞 칸의 변경 태그를 **글자 위치**로 잰다.
  // ★ 개수로 짝지으면 안 된다. 앞 칸(side="old")엔 '삭제될 단어'가, 뒤 칸(side="new")엔
  //   '새로 생긴 단어'가 각각 자기 쪽에만 있어서 박스 개수가 서로 다르다. 순번으로 맞추면
  //   그 차이만큼 앞 칸이 계속 밀린다(실측: STT 쪽 박스가 한 박자 늦게 켜짐).
  //   위치 비율로 맞추면 삭제·삽입이 있어도 같은 대목에서 같이 켜진다.
  function lvMarks(chat, flag) {
    const marks = [];
    let off = 0;
    [...chat.children].forEach((t) => {
      twPrep(t).nodes.forEach((e) => {
        const p = e.node.parentElement;
        if (p && p.hasAttribute('data-diff') && p.dataset.diff.indexOf(flag) >= 0 &&
            (!marks.length || marks[marks.length - 1].el !== p)) marks.push({el: p, off: off});
        off += e.text.length;
      });
    });
    return {marks: marks, total: off};
  }
  // ★ 클래스를 바꾸면 innerHTML 이 바뀐다. twPrep 은 innerHTML 이 지난번에 우리가 그린
  //   모습(__sig)과 다르면 '폴링이 갈아끼웠다'고 보고 캐시를 버리는데, 그러면 **지금 화면에
  //   잘려 있는 글자**를 전문으로 다시 재 버린다 → 말풍선 글자가 거기서 잘린 채 멈춘다.
  //   그래서 우리가 손댄 말풍선은 손댄 뒤에 서명을 다시 남긴다.
  function lvTouch(set, el) { const t = el.closest('.turn'); if (t) set.add(t); }
  function liveSync() {
    const touched = new Set();
    if (!liveSt.mode) {
      document.querySelectorAll('.diffbox span.lv').forEach((e) => {
        e.classList.remove('lv'); lvTouch(touched, e);
      });
      touched.forEach(twSig);
      return;
    }
    const cor = liveSt.mode === 'cor';
    const flag = cor ? 'r' : 'b';
    const sc = lvChat(cor ? 0 : 1);          // 이미 다 나와 있는 앞 칸
    const dc = lvChat(cor ? 1 : 2);          // 지금 타이핑 중인 뒤 칸
    if (!sc || !dc) return;
    // 뒤 칸은 전부 켜 둔다 — 언제 보일지는 타이핑(twRender 의 display)이 정한다.
    dc.querySelectorAll('.bubble span[data-diff*="' + flag + '"]').forEach((e) => {
      if (!e.classList.contains('lv')) { e.classList.add('lv'); lvTouch(touched, e); }
    });
    const d = twState[twKey(dc)];
    const frac = (d && d.total) ? d.shown / d.total : 1;
    const src = lvMarks(sc, flag);
    src.marks.forEach((m) => {
      const on = src.total ? (m.off / src.total) <= frac : true;
      if (m.el.classList.contains('lv') !== on) { m.el.classList.toggle('lv', on); lvTouch(touched, m.el); }
    });
    touched.forEach(twSig);
  }

  function liveDiff(pend) {
    if (!LIVE_ON) return false;
    const c = document.querySelector('.gradio-container') || document.body;
    const manual = c.classList.contains('mode-cor') || c.classList.contains('mode-nor');
    // ★ pend[n] 은 '그 칸이 아직 다 안 나왔다'이지 '지금 나오는 중'이 아니다. 뒤 칸은 앞 칸을
    //   기다리는 동안에도 pend 가 서 있다(typeChats 의 대기 분기). 그래서 뒤 칸을 우선으로
    //   보면 보정 칸이 나오는 내내 pend[2] 가 서 있어 늘 'nor' 로 잡혔다 — STT↔보정 박스가
    //   한 번도 안 뜨고, 보정 칸에는 엉뚱하게 보정↔표준어 박스가 걸리던 원인.
    //   → **앞 칸이 우선**. 지금 실제로 글자가 나오는 칸은 pend 가 선 것 중 맨 앞이다.
    const want = pend[0] ? null : (pend[1] ? 'cor' : (pend[2] ? 'nor' : null));
    let mode = liveSt.mode;
    // ★ 체크 버튼을 켠 순간 실시간 상태는 **버린다**(들고 있다가 돌려주지 않는다).
    //   그래야 버튼을 껐을 때 박스가 전부 사라진다 — 켜기 전에 남아 있던 '보정→표준어'
    //   박스가 되살아나면, 사용자는 끈 적 없는 표시가 다시 뜬 것으로 읽는다.
    //   대화가 아직 나오는 중이라면 아래 want 가 다음 틱에 다시 켜 준다(진행 중 표시는 유지).
    if (manual) mode = null;
    else if (want) mode = want;
    else if (pend[0]) mode = null;      // 새 민원의 전사가 시작됐다 → 지난 민원 상태를 지운다
    // 그 밖에는 그대로 둔다 — 다 나온 뒤에도 박스를 거두지 않는다.
    const changed = mode !== liveSt.mode || manual !== liveSt.manual;
    liveSt.mode = mode; liveSt.manual = manual;
    if (changed) liveApply(c);
    return changed;
  }

  function tick(advance) {
    const sttPending = typeStt(advance);    // STT 칸의 단(전사/로딩/말풍선)을 먼저 정한 뒤에
    // 전사 타이핑·분석중도 'STT 칸이 아직 나오는 중'이다 → 뒤 칸이 그때부터 기다리게 넘긴다
    const seed = sttPending ? {0: true} : null;
    const pend = typeChats(advance, seed);
    // ★ 여기서 판본이 바뀔 수 있다(보정 칸 r 판본 → b 판본). 바뀐 판본의 말풍선은 아직
    //   접혀 있어서, 다음 틱까지 두면 한 틱(≈55ms) 동안 보정 칸이 비어 보인다 → 깜박임.
    //   그래서 바뀐 틱에는 그 자리에서 한 번 더 그린다(advance=false 라 진도는 안 나간다).
    if (liveDiff(pend)) typeChats(false, seed);
    liveSync();                             // 앞 칸 박스를 뒤 칸 타이핑에 맞춰 켠다
    gateAfterNorm(!!pend[2]);               // 표준어 칸이 다 나왔을 때만 결과를 내보낸다
    syncPanes(pend);                        // 반짝임도 '보이는 진행'을 따라간다
  }
  tick(false);
  setInterval(() => tick(true), TICK);

  // ★ 깜빡임 제거: 폴링이 대화 HTML 을 통째로 갈아끼우면 새 말풍선들은 CSS 로 다시 숨겨진
  //   상태로 들어온다. 다음 틱(최대 55ms)까지 기다리면 대화가 사라졌다 다시 뜨는 게 보인다
  //   ('리로드되는 느낌'). MutationObserver 콜백은 브라우저가 그리기 전(마이크로태스크)에
  //   돌기 때문에, 여기서 곧바로 다시 그려주면 숨겨진 상태가 화면에 한 번도 안 나온다.
  //   진행은 올리지 않는다(advance=false) — 안 그러면 DOM 이 바뀔 때마다 타이핑이 빨라진다.
  if (window.MutationObserver) {
    let inTick = false;
    new MutationObserver(() => {
      if (inTick) return;                 // tick 이 만든 변경으로 자신이 다시 불리는 것 방지
      inTick = true;
      try { tick(false); } finally { inTick = false; }
    }).observe(document.body, { childList: true, subtree: true });
  }
"""

# 변경 표시 모드 토글: 보정 칸 체크 → .mode-cor(STT↔보정) / 표준어 칸 체크 →
# .mode-nor(보정↔표준어). 강조색은 둘 다 빨강이고, 둘은 상호배타 — 하나 켜면 다른 하나는 꺼진다.
# 같은 버튼을 다시 누르면 꺼진다. **기본은 둘 다 꺼짐**(강조 없는 원문으로 시작).
# 컨테이너 클래스로 상태를 두므로 폴링이 칩·버튼을 다시 그려도 상태가 유지된다.
_MODE_BODY = """
  const cont = document.querySelector('.gradio-container') || document.body;
  // 보정 칸의 두 판본 중 켜진 모드 쪽만 보여준다. CSS 규칙 대신 인라인 style 로 하는 이유는
  // 위 CSS 주석 참고(Gradio 스코프 때문에 .mode-nor .cview-nor 규칙이 안 먹는다).
  // 폴링이 1초마다 HTML 을 통째로 갈아끼워 인라인 style 이 날아가므로 주기적으로 다시 건다.
  function applyViews() {
    // live-nor(실시간 태그가 '보정→표준어' 차례)일 때도 b 표식 판본을 보여줘야
    // 보정 칸과 표준어 칸에 같은 기준의 박스가 걸린다. 체크 버튼 동작은 그대로다.
    const nor = cont.classList.contains('mode-nor') || cont.classList.contains('live-nor');
    // 판본이 바뀌는 순간엔 타이핑 진행도를 옮겨 준다(위 twMirror 주석). 체크 버튼으로
    // 바꿀 때도 마찬가지다 — 다 나온 대화가 판본만 바뀌었다고 다시 쳐지면 안 된다.
    if (applyViews.__nor !== nor) { applyViews.__nor = nor; twMirror(nor); }
    document.querySelectorAll('.cview-cor').forEach(e =>
      e.style.setProperty('display', nor ? 'none' : 'block', 'important'));
    document.querySelectorAll('.cview-nor').forEach(e =>
      e.style.setProperty('display', nor ? 'block' : 'none', 'important'));
  }
  function setMode(m) {
    cont.classList.toggle('mode-cor', m === 'cor');
    cont.classList.toggle('mode-nor', m === 'nor');
    applyViews();
  }
  setMode(null);    // 기본: 둘 다 꺼짐 — 처음엔 강조 없는 원문으로 보고, 버튼을 눌러야 태그가 뜬다
                    // (보정 칸은 이때 '보정 체크용' 판본을 그대로 보여준다. 모드가 꺼져 있어
                    //  표식에 색이 안 입혀지므로 강조 없는 평범한 대화로 보인다)
  setInterval(applyViews, 250);
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.diff-toggle');
    if (!btn) return;
    const m = btn.dataset.mode;
    setMode(cont.classList.contains('mode-' + m) ? null : m);  // 켜져있으면 끄고, 아니면 켜고 다른건 자동 해제
  });
"""

# 실시간 변경 태그 설정. JS 문자열 안에 f-string 을 쓰면 CSS/JS 의 중괄호가 전부
# 깨지므로, 상수만 따로 만들어 앞에 붙인다.
#   UI_LIVE_DIFF=0        → 끈다 (체크 버튼만 남는다 — 예전 동작)
# 박스는 한 번 켜지면 다음 민원이 시작될 때까지 그대로 둔다(거두지 않는다).
_LIVE_CFG = "  const LIVE_ON = %s;\n" % (
    "true" if os.environ.get("UI_LIVE_DIFF", "1") != "0" else "false",
)

KEEP_SCROLL_JS = "() => {" + _LIVE_CFG + _KEEP_SCROLL_BODY + _MODE_BODY + "}"
FIT_JS = "() => {" + _LIVE_CFG + _KEEP_SCROLL_BODY + _FIT_BODY + _MODE_BODY + "}"

# ── 로그인 화면 꾸미기 ────────────────────────────────────────────────────
# Gradio 의 기본 로그인 화면은 "Login" 제목에 username/password 영문 라벨이 위쪽에 붙어
# 있는 밋밋한 폼이다. 이걸 우리 대시보드와 같은 톤의 카드로 바꾼다.
#
# ■ 왜 auth_message 인가 (css= 도 head= 도 안 통한다)
#   실측: 로그인 페이지가 받는 HTML 에는 launch(css=...) 도 launch(head=...) 도 들어가지
#   않는다(둘 다 문서에서 0건). 적용되는 건 theme= 뿐이다. 반면 auth_message 는 Gradio 가
#   "HTML message provided on login page" 로 명시한 값이라 로그인 화면에 그대로 그려진다
#   → 여기에 <style> 을 함께 실어 보낸다(본문의 <style> 도 브라우저가 적용한다).
#
# ■ 왜 .min-h-screen 아래로만 스코프하나
#   auth_message 는 로그인 화면에만 쓰이지만, 선택자(.form/.block/button.primary)가 흔해서
#   습관적으로 좁혀 둔다. .min-h-screen 은 로그인 래퍼에만 붙는 클래스라 대시보드와 확실히
#   분리된다.
#
# ■ 영문 라벨 교체
#   "Login"/"username"/"password" 는 Gradio 가 박아 넣는 문자열이라 코드로 못 바꾼다.
#   그래서 원문은 font-size:0 으로 감추고 ::before/::after 로 우리 문구를 넣는다.
LOGIN_MESSAGE = """
<style>
/* 이 <style> 은 로그인 페이지에만 실려 가므로(대시보드 HTML 에는 auth_message 가 없다)
   body 같은 넓은 선택자를 써도 대시보드에 영향이 없다. */
/* 배경을 화면 끝까지 채운다 — Soft 테마가 컨테이너에 max-width·여백을 줘서 그냥 두면
   가장자리에 흰 띠가 남는다(실측). */
/* gradio-app(커스텀 엘리먼트)이 불투명 배경을 깔고 있어서 body 에만 칠하면 가려진다(실측:
   gradio-app 의 background 가 rgb(248,250,252)). 둘 다 칠한다. */
html, body { min-height: 100%; }
body, gradio-app {
    margin: 0 !important;
    background: linear-gradient(160deg, #eaeeff 0%, #f6f8fc 52%, #e6f0f7 100%) !important;
}
.gradio-container {
    max-width: 100% !important; width: 100% !important; padding: 0 !important;
    background: transparent !important;
}
.gradio-container > .main { background: transparent !important; }
/* 카드를 화면 정중앙에 */
.wrap.min-h-screen {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 24px; box-sizing: border-box; background: transparent !important;
}
/* 카드 */
.wrap.min-h-screen .column.panel {
    width: 100%; max-width: 396px; box-sizing: border-box;
    background: #ffffff !important;
    border: 1px solid #e4e9f0 !important; border-radius: 18px !important;
    box-shadow: 0 1px 2px rgba(16,24,40,.04), 0 18px 44px rgba(16,24,40,.10) !important;
    padding: 34px 30px 28px !important; gap: 15px !important;
}
/* 제목: 원문("Login") 숨기고 아이콘 + 서비스명 + 부제 */
.wrap.min-h-screen h2 {
    font-size: 0 !important; text-align: center; margin: 0 !important; line-height: 1.35;
}
.wrap.min-h-screen h2::after {
    content: "🎙️ 악취 민원 사투리 STT 보정 파이프라인";
    display: block; font-size: 18.5px; font-weight: 800; letter-spacing: -.02em;
    color: #17212f;
}
/* 폼 묶음의 Gradio 기본 테두리·배경 제거 (필드 두 개만 깔끔하게) */
.wrap.min-h-screen .form {
    border: 0 !important; background: transparent !important;
    display: flex !important; flex-direction: column; gap: 13px !important;
    overflow: visible !important;
}
.wrap.min-h-screen .form > .block {
    padding: 0 !important; border: 0 !important; background: transparent !important;
    box-shadow: none !important; min-width: 0 !important;
}
/* 라벨: 영문 숨기고 한글로. 칩 모양(배경)도 없애 평범한 라벨로 */
.wrap.min-h-screen .form label > span {
    font-size: 0 !important; background: none !important; padding: 0 0 6px !important;
    display: block !important; border: 0 !important;
}
.wrap.min-h-screen .form > .block:nth-of-type(1) label > span::before { content: "아이디"; }
.wrap.min-h-screen .form > .block:nth-of-type(2) label > span::before { content: "비밀번호"; }
.wrap.min-h-screen .form label > span::before {
    font-size: 13px; font-weight: 700; color: #6c7a90; letter-spacing: .01em;
}
/* 입력칸 */
.wrap.min-h-screen .form label { background: transparent !important; border: 0 !important; }
.wrap.min-h-screen .form .input-container { border-radius: 11px !important; }
.wrap.min-h-screen .form input {
    font-size: 15px !important; padding: 11px 13px !important;
    border: 1px solid #dde3ec !important; border-radius: 11px !important;
    background: #f9fbfd !important; color: #17212f !important;
    transition: border-color .15s, box-shadow .15s, background .15s;
}
.wrap.min-h-screen .form input:focus {
    outline: none !important; background: #fff !important;
    border-color: #7d8bec !important; box-shadow: 0 0 0 3px rgba(79,95,216,.15) !important;
}
/* 버튼: 원문("Login") 숨기고 '로그인' */
.wrap.min-h-screen button.primary {
    width: 100%; margin-top: 4px;
    font-size: 0 !important; padding: 12px 16px !important;
    border: 0 !important; border-radius: 12px !important;
    background: linear-gradient(180deg, #5b6ce0, #4a59d0) !important;
    box-shadow: 0 6px 16px rgba(74,89,208,.28) !important;
    transition: transform .12s, box-shadow .15s, filter .15s;
}
.wrap.min-h-screen button.primary::before {
    content: "로그인"; font-size: 16px; font-weight: 750; color: #fff; letter-spacing: .01em;
}
.wrap.min-h-screen button.primary:hover { filter: brightness(1.05);
    box-shadow: 0 9px 20px rgba(74,89,208,.34) !important; }
.wrap.min-h-screen button.primary:active { transform: translateY(1px); }
/* 카드 하단 안내 */
.wrap.min-h-screen .column.panel::after {
    content: "허가된 사용자만 접속할 수 있습니다";
    display: block; text-align: center; margin-top: 2px;
    font-size: 12.5px; color: #93a0b4;
}
/* 다크 모드 */
.dark body, .dark gradio-app {
    background: linear-gradient(160deg, #171b22 0%, #1b2029 55%, #151a24 100%) !important;
}
.dark .wrap.min-h-screen .column.panel {
    background: #171b22 !important; border-color: #2b313c !important;
    box-shadow: 0 1px 2px rgba(0,0,0,.3), 0 18px 44px rgba(0,0,0,.4) !important;
}
.dark .wrap.min-h-screen h2::after { color: #e5eaf1; }
.dark .wrap.min-h-screen .form label > span::before { color: #8a95a7; }
.dark .wrap.min-h-screen .form input {
    background: #1e222b !important; border-color: #2b313c !important; color: #e5eaf1 !important;
}
.dark .wrap.min-h-screen .form input:focus { background: #232833 !important; border-color: #5b6ce0 !important; }
/* auth_message 자체가 그려지는 자리 — 제목 아래 부제로 쓴다 */
.wrap.min-h-screen .lp-login-sub {
    text-align: center; margin: -4px 0 2px;
    font-size: 13.5px; color: #6c7a90; line-height: 1.5;
}
.dark .wrap.min-h-screen .lp-login-sub { color: #8a95a7; }
</style>"""

THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.indigo,
    secondary_hue=gr.themes.colors.blue,
    neutral_hue=gr.themes.colors.slate,
    radius_size=gr.themes.sizes.radius_lg,
    # 폰트는 이름만 준다 — GoogleFont 로 주면 시연 중 외부 네트워크를 타고, 막히면 깨진다.
    font=["ui-sans-serif", "system-ui", "Segoe UI", "Apple SD Gothic Neo",
          "Malgun Gothic", "Noto Sans KR", "sans-serif"],
)

with gr.Blocks(title="사투리 STT 보정 파이프라인", fill_width=True) as demo:
    # 제목과 진행 줄을 한 박스에 담는다 — 4px 막대만 카드 밖에 떠 있으면 붕 뜬 것처럼 보인다
    with gr.Column(elem_classes="head-card"):
        gr.HTML(
            '<div class="app-head">'
            '<h1 class="app-title">🎙️ 악취 민원 사투리 STT 보정 파이프라인</h1>'
            '</div>'
        )
        progress = gr.HTML(progress_html(-1), elem_classes="topbar-holder")

    # 한 화면에: STT 결과 → 사투리 보정 → 표준어 변환 (단계 간 바뀐 단어 강조) + 키워드.
    # 키워드는 표준어 변환 오른쪽에 얇은 카드로 붙는다 (scale 로 폭을 좁게: 텍스트 2 : 키워드 1).
    with gr.Row(elem_classes="pane-row", equal_height=True):
        # 칩 + 체크버튼을 한 gr.HTML(section_block) 안에 담는다 — 별도 컴포넌트로 gr.Row 에
        # 나누면 Gradio 가 폭을 갈라 칩 글자가 세로로 접힌다.
        with gr.Column(elem_classes="pane", scale=2):
            sec_stt = gr.HTML(section_block(0, -1), elem_classes="chip-line")
            stt_box = gr.HTML("", elem_classes="diffbox")
        with gr.Column(elem_classes="pane", scale=2):
            sec_corrected = gr.HTML(section_block(1, -1), elem_classes="chip-line")
            corrected_box = gr.HTML("", elem_classes="diffbox")
        with gr.Column(elem_classes="pane", scale=2):
            sec_normalized = gr.HTML(section_block(2, -1), elem_classes="chip-line")
            normalized_box = gr.HTML("", elem_classes="diffbox")
        # 키워드 카드. 옆 3칸보다 좁게(scale=1) 두되, 위에 지도가 들어가므로 너무 좁으면
        # 지도가 못 읽힐 정도로 작아진다 → min_width 로 최소 폭을 확보한다(실측: 205px 면
        # 지도가 175px 밖에 안 됨).
        with gr.Column(elem_classes=["pane", "pane-kw"], scale=1, min_width=300):
            sec_keywords = gr.HTML(section_html(*SECTIONS[3], -1, sec=3), elem_classes="chip-line")
            with gr.Column(elem_classes="kw-list"):
                # 지도가 위(남은 세로를 다 채움), 키워드 5개는 아래 묶음에.
                # 지도를 세 조각으로 나눠 둔다 — 범례(고정) / 전국 지도(고정) / 확대 지도(숨김).
                # 폴링이 갈아끼우는 건 확대 칸뿐이고 그건 숨어 있으므로, 대화가 흐르는 중에
                # 지도가 다시 그려지며 번쩍이는 일이 없다.
                with gr.Column(elem_classes="kw-map-slot"):
                    gr.HTML(MAP_LEGEND, elem_classes="kw-legend-slot")
                    kw_map = gr.HTML("", elem_classes=["kw-mapview", "kw-map-idle"])
                    kw_zoom = gr.HTML("", elem_classes=["kw-mapview", "kw-map-zoom"])
                with gr.Column(elem_classes="kw-kwbox"):
                    kw_boxes = [gr.HTML(kw_html(k, "", i), elem_classes="kw-slot")
                                 for i, k in enumerate(KW_LABELS)]

    # 토스트 전용 홀더(맨 아래). position:fixed라 화면에 뜨고, 제목~진행칩 사이 공간은 안 만든다.
    notice = gr.HTML("", elem_classes="notice-holder")

    all_outputs = [
        notice,
        progress,
        stt_box, corrected_box, normalized_box,
        kw_map, kw_zoom, *kw_boxes,
        sec_stt, sec_corrected, sec_normalized, sec_keywords,
    ]

    # 1초마다 폴링. 파이프라인 단계가 수 초 단위라 1초면 충분히 즉각적이고 화면도 차분하다.
    # 더 빠르게/느리게 하려면 UI_POLL_SEC 로 조절 (예: 0.3=더 빠름, 2=더 느림).
    _poll_sec = float(os.environ.get("UI_POLL_SEC", "1.0"))
    # 세션별 '직전에 보낸 값'. 이게 있어야 새로 연 탭·새로고침한 페이지도 첫 틱에
    # 전체 값을 받는다 (전역이면 gr.skip() 만 받아 빈 화면으로 남는다).
    sent_state = gr.State({})

    timer = gr.Timer(_poll_sec)
    timer.tick(fn=poll, inputs=[sent_state], outputs=[sent_state, *all_outputs])

    # 페이지가 뜨면 스크립트를 심는다 (Blocks 에는 head/js 인자가 없다).
    # UI_FIT=0 이면 세로 자동맞춤을 끄고 CSS 고정 높이(.diffbox)만 쓴다 — 자동맞춤이
    # 의심될 때 변수를 하나 줄여서 확인하는 용도.
    demo.load(fn=None, inputs=None, outputs=None,
              js=FIT_JS if os.environ.get("UI_FIT", "1") != "0" else KEEP_SCROLL_JS)


if __name__ == "__main__":
    port = 7860

    # 보안 기본값: 127.0.0.1(localhost) 바인딩 → SSH 포트포워딩으로만 접근.
    # HOST=0.0.0.0 이면 네트워크에 노출된다(로그인 노드 프록시가 붙을 수 있게 하려면 필요).
    host = os.environ.get("HOST", "127.0.0.1")
    share = os.environ.get("SHARE", "0") == "1"
    local_only = host in ("127.0.0.1", "localhost")

    # ── root_path: 어떤 프록시 뒤에 있는지에 따라 다르다 ──────────────
    # ① UI_ROOT_PATH=/ui  → 로그인 노드 8000 프록시의 `/ui` 경로 뒤 (scripts/proxy.py).
    #    프록시가 `/ui` 를 떼고 넘기므로, 앱이 생성 URL 에 다시 붙여야 자산·SSE 가 맞는다.
    # ② JF_POD_* 가 있으면 → JupyterFlow 프록시 뒤 (전체 URL 형태).
    # ①이 있으면 ①을 쓴다 — 둘 다 걸린 환경에서 조용히 JF 쪽이 이기면 원인을 못 찾는다.
    root_path = os.environ.get("UI_ROOT_PATH") or None
    if root_path is None:
        pod_name = os.environ.get("JF_POD_NAME")
        pod_index = os.environ.get("JF_POD_INDEX")
        public_host = os.environ.get("JF_PUBLIC_HOST", "")
        public_port = os.environ.get("JF_PUBLIC_PORT", "")
        root_path = (
            f"http://{public_host}:{public_port}/vscode/{pod_name}-{pod_index}/proxy/{port}"
            if public_host and public_port and pod_name and pod_index is not None else None
        )
    print(f"[app_ui_live] root_path={root_path}", flush=True)

    # ── 로그인 ─────────────────────────────────────────────────────
    # 비밀번호는 환경변수로만 받는다 (코드에 기본값을 두지 않는다).
    # 외부에 노출되는 구간에서는 반드시 값을 주고, 쓰지 않을 때는 닫을 것.
    ui_user = os.environ.get("UI_USER", "piai")
    ui_pass = os.environ.get("UI_PASS", "")
    if not ui_pass:
        if not local_only or share:
            raise SystemExit(
                f"[app_ui_live] 중단: HOST={host}(share={share}) 로 외부에 노출되는데 "
                f"UI_PASS 가 없다.\n"
                f"  export UI_USER=<계정> UI_PASS=<비밀번호>  를 먼저 설정할 것.\n"
                f"  (localhost 바인딩 + SSH 포워딩으로만 쓸 때는 인증 없이 띄울 수 있다)")
        print("[app_ui_live] 로그인 인증: OFF — localhost 전용이라 허용한다", flush=True)
    auth = (ui_user, ui_pass) if ui_pass else None
    src = LIVE_PROGRESS_URL or f"파일 {Path(os.environ.get('LIVE_PROGRESS_PATH', A_ROOT / 'data' / 'live_progress.json'))}"
    print(f"[app_ui_live] 진행상황 출처: {src} · 폴링 {_poll_sec}초", flush=True)
    print(f"[app_ui_live] 바인딩 {host}:{port} · 인증 "
          f"{'ON (user=' + ui_user + ')' if auth else 'OFF'}", flush=True)

    demo.launch(
        server_name=host, server_port=port, root_path=root_path,
        share=share, auth=auth,
        theme=THEME, css=CSS,
        # 로그인 화면 꾸미기. css=/head= 는 로그인 페이지에 안 들어가서 auth_message 로
        # <style> 과 함께 실어 보낸다 (위 LOGIN_MESSAGE 주석 참고).
        auth_message=LOGIN_MESSAGE if auth else None,
    )
