# app/core/lifespan.py  (서버 A)
"""
서버 A 수명주기 + 잡 처리 함수
- 기존 구조(업로드 → 인메모리 큐 → 워커 스레드)는 유지하고,
  처리부만 자립형 파이프라인(A/pipeline_src)으로 교체했다.
- 처리 흐름 (process_fn):
    1) 업로드 바이트 → ffmpeg 디코딩 (float32 mono 16kHz numpy)
    2) Whisper large-v3 STT (transformers pipeline)
    3) LangGraph 보정 파이프라인 (지역 추정 → RAG 지명 보정 → 잔여 오인식 보정 → 표준어 변환 → 키워드 추출)
       ※ 메인 LLM(Qwen3.6-27B, 8001) vLLM 필요. 키워드 LLM(0.6B, 8002)은 KEYWORDS_VERSION=1일 때만.
    4) 학습용 원천 데이터 저장 (오디오 / STT 원문 / 보정 텍스트 / 키워드)
    5) 민원 발생지 LLM 정제 후 서버 B로 결과 전송
- Whisper·임베딩(ko-sroberta) 모델은 원래 첫 요청 시 lazy 로드였으나, 첫 통화가
  로드 지연을 겪지 않도록 서버 기동 시(lifespan startup)에 미리 로드해둔다.
"""
import os
import re
import threading
import time
import unicodedata
import uuid
import difflib
import html as _html
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.core.config import (
    SERVER_B_URL, KEYWORDS_VERSION, BASE_SAVE_DATA_DIR, DEFAULT_CONTACT,
)
from app.core.worker import start_worker, stop_worker, set_processor
from app.core.server_graph import build_server_graph, make_initial_state
from app.core.stt import transcribe_numpy
from app.core.keywords import parse_keywords
from app.core.naver_api import naver_local_search, geocode_address
from app.audio.decoder import decode_audio_bytes_to_numpy
from core.config import get_whisper_pipeline, get_embedder
from core.search import warmup_regions
from core.llm import llm, _get_silero
from pipeline import prompts
from app.core.live_progress import write_progress, build_snapshot, NODE_STEP


def send_result_to_server_b(payload: dict):
    with httpx.Client(timeout=10) as client:
        r = client.post(SERVER_B_URL, json=payload)
        r.raise_for_status()


def now_kst_str() -> str:
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y-%m-%d_%H-%M-%S")


_TOK = re.compile(r"\s+|\S+")
# 화자 라벨(상담원:/민원인:)은 변경 강조 대상에서 제외
_SPEAKER = re.compile(r"^(상담원|민원인)\s*[:：]?$")
# 토큰 끝에 붙은 종결기호(. , ? !). 비교 시 떼어내고, 강조 시에도 기호엔 색을 넣지 않는다.
_TRAIL_PUNCT = re.compile(r"[.,?!]+$")

# STT 전사 완료 후 "민원 분석중…" 스피너를 최소한 이만큼은 보여주고 다음 단계로 넘어간다.
# 이 시간 안에 UI 가 (1) 남은 타이핑 마무리(≈1초) → (2) 스피너 표시 를 끝내야 하고,
# 폴링이 1초라 최소 2번은 폴링돼야 화면에 확실히 잡힌다. 그래서 3초 정도로 잡는다.
# (화면이 '바로 넘어가지' 않게 해주는 텀 역할도 한다)
_ANALYZING_MIN_SEC = float(os.environ.get("ANALYZING_MIN_SEC", "3.0"))

# A는 '내용 HTML'만 만든다 (바뀐 단어 빨간 강조). 박스 모양(높이·테두리·스크롤·줄바꿈)은
# UI의 .diffbox CSS가 담당 → 높이 등 조절은 UI만 고치면 된다.


def wrap_html(text: str) -> str:
    """일반 텍스트 → HTML (하이라이트 없음). 줄바꿈은 UI의 pre-wrap이 렌더."""
    return _html.escape(text or "")


def _should_color(tok: str) -> bool:
    """토큰을 변경 강조할지 여부. 공백·화자 라벨은 제외.
    (종결기호만 다른 변경은 diff_html의 키 비교에서 이미 '동일'로 처리되므로 여기선 안 뺀다.)"""
    t = tok.strip()
    if not t:
        return False
    if _SPEAKER.match(t):
        return False
    return True


def _diff_key(tok: str) -> str:
    """변경 비교용 정규화: 끝의 종결기호(.,?!)를 떼어 '기호만 다른 것'은 같은 토큰으로 취급."""
    return _TRAIL_PUNCT.sub("", tok)


_WS_INLINE = re.compile(r"^[^\S\n]+$")   # 줄바꿈 없는 공백만 (줄바꿈은 안 이어붙인다)


def _emit_run(text: str, flag) -> str:
    """연속 토큰을 이어붙인 text 한 덩어리 → HTML.
    flag 는 이 덩어리가 '어느 변경에 속하는지' 표식이다 (색이 아니다 — 색은 UI 가 모드에
    따라 입힌다): None=변경없음 / "r"=STT↔보정 변경 / "b"=보정↔표준어 변경 / "rb"=둘 다.
    강조할 땐 덩어리 전체를 span 하나로 감싸고 맨 끝 종결기호(.,?!)는 span 밖으로 뺀다."""
    if not flag:
        return _html.escape(text)
    m = _TRAIL_PUNCT.search(text)
    stem, punct = (text[:m.start()], text[m.start():]) if m else (text, "")
    return f'<span data-diff="{flag}">{_html.escape(stem)}</span>{_html.escape(punct)}'


def _render(tokens, flags) -> str:
    """토큰별 표식(None=변경없음)을 받아 HTML. 붙어 있는 같은 표식 토큰은 span 하나로 병합
    → 연속으로 바뀐 단어가 낱개 배지가 아니라 '한 박스'로 보인다.
    같은 표식 두 단어 사이의 공백은 그 표식으로 이어 붙여 박스가 끊기지 않게 한다. 단 줄바꿈은
    잇지 않는다 — UI(chat_html)가 줄 단위로 쪼개므로 span 이 줄을 넘으면 태그가 깨진다."""
    flags = list(flags)
    for i, t in enumerate(tokens):
        if flags[i] is None and 0 < i < len(tokens) - 1 and _WS_INLINE.match(t):
            fL, fR = flags[i - 1], flags[i + 1]
            # 양옆이 같은 표식이면 사이 공백도 그 표식으로 채워 한 박스로 잇는다
            # ("몬 살겠" 처럼 띄어쓰기를 사이에 둔 연속 변경이 두 박스로 갈라지지 않게).
            if fL and fL == fR:
                flags[i] = fL
    parts, i, n = [], 0, len(tokens)
    while i < n:
        f, j = flags[i], i
        while j < n and flags[j] == f:
            j += 1
        parts.append(_emit_run("".join(tokens[i:j]), f))
        i = j
    return "".join(parts)


def _strip_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _diff_flags(a_toks, b_toks, side: str):
    """(emit할 토큰들, 각 토큰이 '실제로' 바뀌었나 bool). side="new"면 b 쪽, "old"면 a 쪽을 emit.
    한 블록의 공백을 제거하면 양쪽이 같은 경우(띄어쓰기만 다름)는 '안 바뀜'으로 본다 —
    이러면 "못살겠어예"↔"못 살겠어예" 같은 띄어쓰기 변화는 박스가 안 붙는다.
    (종결기호만 다른 것은 _diff_key 가 이미 같은 토큰으로 만들어 'equal' 로 걸러진다.)"""
    ka = [_diff_key(t) for t in a_toks]
    kb = [_diff_key(t) for t in b_toks]
    sm = difflib.SequenceMatcher(a=ka, b=kb, autojunk=False)
    toks, flags = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        real = _strip_ws("".join(a_toks[i1:i2])) != _strip_ws("".join(b_toks[j1:j2]))
        if side == "old":
            changed = real and op in ("delete", "replace")
            span = a_toks[i1:i2]
        else:
            changed = real and op in ("insert", "replace")
            span = b_toks[j1:j2]
        for tok in span:
            toks.append(tok)
            flags.append(changed)
    return toks, flags


def diff_html(old: str, new: str, flag: str = "r", side: str = "new") -> str:
    """두 텍스트의 차이를 표식(data-diff)으로 감싼 HTML. 색은 UI 가 모드에 따라 입힌다.
    - side="new"(기본): new 를 그리며 old 대비 바뀐 단어에 flag (표준어 칸 → flag="b").
    - side="old": old 를 그리며 new 로 가며 교체·삭제될 단어에 flag (STT 칸 → flag="r").
    - 띄어쓰기만·종결기호만 다른 변경은 표식 안 함 / 화자 라벨(상담원:/민원인:)도 안 함.
    - 붙어 있는 변경 단어는 _render 가 span 하나(한 박스)로 묶는다."""
    toks, changed = _diff_flags(_TOK.findall(old or ""), _TOK.findall(new or ""), side)
    vals = [flag if (c and _should_color(t)) else None for t, c in zip(toks, changed)]
    return _render(toks, vals)


class StageTimer:
    """한 통화의 단계별 소요시간을 재서 마지막에 한 줄씩 찍는다.

    왜 필요한가: 27B LLM 호출이 파이프라인 안에 여러 번 있어서, 한 통화가 오래 걸릴 때
    **어디가 느린지**(STT? 그래프? 지오코딩?)를 로그만 보고는 알 수 없었다. 배포 후에는
    프로파일러를 붙일 수 없으니 상시 계측을 넣는다.

    `_ANALYZING_MIN_SEC` 대기(UI 가 스피너를 보여줄 시간)는 우리가 일부러 넣은 지연이라
    따로 표시한다 — 실제 처리 시간과 섞이면 최적화 판단을 흐린다."""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.marks = []          # [(단계명, 초)]
        self._last = self.t0

    def mark(self, name: str):
        now = time.perf_counter()
        self.marks.append((name, now - self._last))
        self._last = now

    @staticmethod
    def _pad(name: str, width: int = 26) -> str:
        """한글은 터미널에서 두 칸을 차지한다. f"{n:<22s}" 는 글자 수로만 세므로 한글이
        섞이면 열이 어긋나 로그가 읽기 어려워진다. 표시 폭으로 직접 맞춘다."""
        w = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in name)
        return name + " " * max(1, width - w)

    def report(self, fname: str, ok: bool = True, error: str = "") -> float:
        total = time.perf_counter() - self.t0
        head = "처리 완료" if ok else "처리 실패"
        # total 이 0 에 가까우면 비율 계산이 터진다 (아주 짧은 실패 경로).
        pct = (lambda sec: f"({sec / total * 100:4.1f}%)") if total > 0.001 else (lambda sec: "")
        lines = [f"[TIMING] ===== {head}: {fname} =====",
                 *(f"[TIMING]   {self._pad(n)}{sec:7.2f}초  {pct(sec)}"
                   for n, sec in self.marks),
                 f"[TIMING]   {'-' * 42}",
                 f"[TIMING]   {self._pad('전체 처리시간')}{total:7.2f}초"]
        if not ok:
            lines.append(f"[TIMING]   중단 원인: {error}")
        print("\n".join(lines), flush=True)
        return total


def save_raw_dataset(job, stt_text: str, normalized_text: str, parsed: dict) -> str:
    """학습용 원천 데이터 저장 (오디오/STT/보정 텍스트/키워드)."""
    save_dir = os.path.join(BASE_SAVE_DATA_DIR, now_kst_str())
    os.makedirs(save_dir, exist_ok=True)

    ext = Path(job.filename).suffix.lower() or ".bin"
    with open(os.path.join(save_dir, f"audio{ext}"), "wb") as f:
        f.write(job.data)
    with open(os.path.join(save_dir, "stt.txt"), "w", encoding="utf-8") as f:
        f.write(stt_text)
    with open(os.path.join(save_dir, "normalized.txt"), "w", encoding="utf-8") as f:
        f.write(normalized_text)
    with open(os.path.join(save_dir, "keywords.txt"), "w", encoding="utf-8") as f:
        for k, v in parsed.items():
            f.write(f"{k}: {v}\n")
    return save_dir


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 파이프라인 그래프 컴파일
    graph = build_server_graph(keyword_version=KEYWORDS_VERSION)
    app.state.graph = graph

    # Whisper·임베딩 모델 미리 로드 (첫 통화 처리 시 GPU 로드 지연 방지)
    print("[STARTUP] Whisper large-v3 로딩 중...")
    get_whisper_pipeline()
    print("[STARTUP] Whisper 로드 완료")
    print("[STARTUP] 임베딩 모델(ko-sroberta) 로딩 중...")
    get_embedder()
    print("[STARTUP] 임베딩 모델 로드 완료")
    print("[STARTUP] Silero VAD(발화) 로딩 중...")
    try:
        _get_silero()
        print("[STARTUP] Silero VAD(발화) 로드 완료")
    except Exception as e:
        print(f"[STARTUP][WARN] Silero VAD(발화) 로드 실패(첫 요청 시 재시도): {e}")

    # 지명 FAISS + 어휘/자모 희소색인 예열. 경상도는 load_faiss 4.2초 + 색인 8.3초라
    # 예열하지 않으면 그 지역 첫 민원이 12초를 그냥 문다.
    # 백그라운드 스레드로 돌린다 — 예열이 끝나기 전에 들어온 요청도 lazy 경로로
    # 정상 처리되고(같은 락을 공유해 중복 구축은 없다), 서버 기동만 지연되지 않는다.
    def _warm_faiss():
        print("[STARTUP] 지명 FAISS + 희소색인 예열 시작 (백그라운드, 약 4.3GB)")
        warmup_regions()
        print("[STARTUP] 지명 FAISS + 희소색인 예열 완료")

    threading.Thread(target=_warm_faiss, name="faiss-warmup", daemon=True).start()

    def process_fn(job):
        fname = job.filename
        timer = StageTimer()         # 단계별 소요시간 → 끝에서 [TIMING] 로 한꺼번에 출력
        job_token = uuid.uuid4().hex  # 통화마다 고유 → 7860 UI가 '새 민원' 감지·알림용
        # 진행상황 파일 초기화 (7860 UI가 실시간 단계별로 읽어감): STT 실행 중
        write_progress(build_snapshot(0, make_initial_state("", audio_file=fname), file=fname, job=job_token))

        # 1) 디코딩
        ext = Path(fname).suffix.lower()
        audio_np, sr = decode_audio_bytes_to_numpy(job.data, ext=ext, target_sr=16000)
        if audio_np.size == 0:
            raise RuntimeError("Decoded audio is empty")
        timer.mark("오디오 디코딩")

        # 2) STT (Whisper large-v3)
        # 청크가 끝날 때마다 누적 전사문을 step 0 스냅샷의 stt_stream 으로 흘려보낸다 →
        # UI(7860)가 STT 칸에 LLM 답변처럼 단어 단위로 타이핑해 보여준다. STT 가 끝나 파이프라인
        # (step 1~)이 돌기 시작하면 stt_stream 은 안 보내므로 UI 는 자동으로 원래(말풍선) 표시로 바뀐다.
        _stt_base = make_initial_state("", audio_file=fname)

        def _on_stt_progress(partial):
            write_progress(build_snapshot(0, _stt_base, file=fname, job=job_token,
                                          extra={"stt_stream": partial}))

        stt_text = transcribe_numpy(audio_np, sampling_rate=sr, on_progress=_on_stt_progress)
        timer.mark("STT (Whisper)")
        # 오디오 길이 대비 STT 속도(RTF)를 같이 남긴다 — 통화가 길어서 느린 것과 서버가
        # 느린 것을 구분할 수 있어야 한다.
        _audio_sec = audio_np.size / float(sr or 16000)
        print(f"[STT 원문]\n{stt_text}")
        print(f"[TIMING] 오디오 길이 {_audio_sec:.1f}초", flush=True)

        # STT 전체 전사가 끝났다 → 이제 화자분리(상담원/민원인) 등 '분석'에 들어간다.
        # 분석중 스냅샷에 **전체 전사문(stt_stream)을 그대로 함께** 실어 보낸다: UI 는 마지막
        # 청크까지 타이핑을 끝낸 '뒤에' "민원 분석중…" 스피너로 바꾼다(전문을 안 끊고 다 보여줌).
        # step 1 스냅샷(말풍선)이 오면 자동으로 대체된다.
        if stt_text:
            write_progress(build_snapshot(0, _stt_base, file=fname, job=job_token,
                                          extra={"stt_stream": stt_text, "stt_analyzing": True}))
            # UI 가 남은 타이핑을 몰아치고 스피너로 바꿀 시간을 준다. 이게 없으면 다음 단계
            # (remove_noise)가 빨리 끝나는 경우 step 1 스냅샷이 곧바로 덮어써서 스피너가
            # 한 프레임도 안 보이고 지나간다. UI 폴링이 1초라 그보다 넉넉히 잡는다.
            time.sleep(_ANALYZING_MIN_SEC)
            timer.mark("UI 대기(의도된 지연)")

        # 3) 보정 파이프라인 (지역추정 → RAG 지명보정 → 표준어 변환 → 키워드 추출)
        #    graph.stream 으로 노드별 결과를 받아 진행상황 파일에 단계별로 기록한다.
        state = make_initial_state(stt_text, audio_file=fname)

        # 표시용 대화 정리. 사투리보정(corrected_final)·표준어변환(normalized_text)은
        # 파이프라인에서 이미 화자분리(상담원/민원인)된 대화 형태로 확정되므로 여기선 재포맷하지 않고
        # 그대로 쓴다. STT 원문만 표시용으로 화자분리(1회 LLM 호출·캐시)한다.
        # _fmt_stt는 첫 호출 시점의 stt_text를 캐시하므로, 첫 _disp() 호출이 STEP1.5(remove_noise)
        # 완료 '이후'(그래프 루프 안, 아래)에 일어나도록 여기서는 미리 호출하지 않는다 —
        # 그래야 화면의 "STT 원문"이 환각/반복 제거가 끝난 텍스트를 기준으로 화자분리된다.
        _disp_cache = {}

        def _fmt_stt(raw):
            if not raw:
                return raw
            if "stt" not in _disp_cache:
                try:
                    out = llm([{"role": "user", "content": prompts.format_dialogue_prompt(raw)}]) or raw
                except Exception as e:
                    print(f"[FORMAT][WARN] stt: {e}")
                    out = raw
                _disp_cache["stt"] = "\n\n".join(ln for ln in out.split("\n") if ln.strip())
            return _disp_cache["stt"]

        def _disp(st):
            # 3개 텍스트를 화자 라벨+줄바꿈 상태로, 단계 간 바뀐 단어를 표식(data-diff)으로
            # 감싼 HTML 로 저장. 표식은 '어느 전환에 속하는지'만 뜻하고 색은 UI 가 입힌다
            # (지금은 두 전환 모두 빨강, 체크 버튼으로 한 번에 하나만 본다):
            #   "r" = STT ↔ 보정 전환   / "b" = 보정 ↔ 표준어 전환
            #   STT 칸    : 보정으로 바뀔 원본 단어에 "r" (side="old")
            #   보정 칸    : **두 벌**을 따로 그린다 (아래 참고)
            #   표준어 칸  : 보정 대비 바뀐 단어에 "b"
            #
            # 보정 칸을 두 벌로 그리는 이유 — 붙어 있는 글자의 '박스 연결' 때문이다.
            # 한 벌에 r/b/rb 를 섞으면 인접한 두 단어의 표식이 달라질 수 있고(예: "몬"=r,
            # "살겠"=rb), _render 는 같은 표식만 한 span 으로 묶으므로 **한 모드에선 둘 다
            # 빨간데 박스는 끊겨 보인다.** 모드별로 따로 그리면 각 벌의 표식이 '바뀜/안바뀜'
            # 두 값뿐이라 인접한 변경 단어가 항상 한 박스로 묶인다.
            d = dict(st)
            f_stt = _fmt_stt(st.get("stt_text", ""))
            f_cor = st.get("corrected_final")        # 파이프라인에서 이미 화자분리됨
            f_nor = st.get("normalized_text")        # 파이프라인에서 이미 화자분리+변환됨
            d["stt_text"] = diff_html(f_stt, f_cor, flag="r", side="old") if f_cor else wrap_html(f_stt)
            if f_cor:
                # 보정 체크용: 보정문을 그리며 STT 대비 바뀐 단어에 "r"
                d["corrected_final"] = diff_html(f_stt, f_cor, flag="r", side="new")
                # 표준어 체크용: 같은 보정문을 그리며 표준어로 바뀔 단어에 "b"
                d["corrected_final_nor"] = (
                    diff_html(f_cor, f_nor, flag="b", side="old") if f_nor else "")
            if f_nor:
                d["normalized_text"] = diff_html((f_cor or f_stt), f_nor, flag="b", side="new")
            return d

        # STEP1.5(remove_noise)가 그래프의 첫 노드이므로, 그 완료 시점에 루프 안에서
        # NODE_STEP["remove_noise"]=1 로 첫 write_progress(_disp(...))가 자동 발생한다.
        # (그 전까지는 STT 진행중 표시(step 0)만 있고, "STT 원문" 화면 텍스트는 아직 안 채워짐)

        # 지도: 민원이 들어오면 UI 가 곧바로 '빈 지도(서비스 지역)'를 띄우고, 키워드 추출이
        # 끝나 정밀 좌표(rep_*/odor_*)가 나오면 그 위에 마커를 얹는다. 그래서 여기 루프에서는
        # 지도 관련해 따로 할 일이 없다(빈 지도는 UI 가 file 유무만 보고 알아서 띄운다).
        result = dict(state)
        try:
            for update in graph.stream(state):
                for node_name, node_out in update.items():
                    result = {**result, **(node_out or {})}  # 부분 반환 노드도 있어 누적 병합
                    step = NODE_STEP.get(node_name)
                    if step is not None:
                        write_progress(build_snapshot(step, _disp(result), file=fname, job=job_token))
        except Exception as e:
            write_progress(build_snapshot(NODE_STEP.get("extract_keywords", 8), _disp(result),
                                          error=str(e), file=fname, job=job_token))
            # 실패해도 여기까지 걸린 시간은 남긴다 — 타임아웃으로 죽은 건지 즉시 터진 건지
            # 구분해야 원인을 좁힐 수 있다.
            timer.mark("보정 파이프라인(실패)")
            timer.report(fname, ok=False, error=str(e))
            raise
        timer.mark("보정 파이프라인(LLM)")

        normalized_text = result.get("normalized_text", "") or stt_text
        keywords_str = result.get("keywords", "")
        parsed = parse_keywords(keywords_str, version=KEYWORDS_VERSION)
        print(f"[키워드 추출 결과]\n{parsed}")
        timer.mark("키워드 파싱")

        # 신고자 위치 / 악취(냄새) 위치를 각각 지오코딩한다.
        # 지역명(시·군)은 LLM이 자주 누락하므로 여기서 확정적으로 붙인다(이미 있으면 안 붙임).
        region_prefix = result.get("estimated_subregion") or result.get("estimated_region") or ""

        def attach_region(text):
            t = (text or "").strip()
            if not t or t in ("미언급", "모름") or not region_prefix or region_prefix in t:
                return t
            return f"{region_prefix} {t}"

        reporter_text = attach_region(parsed["location"])        # 신고자 위치 (지역명 부착)
        odor_text = attach_region(parsed["suspected_source"])    # 냄새 위치 (지역명 부착)
        matches = result.get("place_matches", [])     # 도로명 폴백용 (RAG 매칭 결과)

        def road_address_for(place_text):
            p = (place_text or "").strip()
            if not p or p in ("미언급", "모름"):
                return ""
            # 위치 텍스트에 포함된 지명 중 가장 구체적인(긴) 매칭의 도로명을 사용
            best_road, best_len = "", 0
            for m in matches:
                picked, road = m.get("picked", ""), m.get("road_address", "")
                if picked and picked != "없음" and road and picked in p and len(picked) > best_len:
                    best_road, best_len = road, len(picked)
            return best_road

        def naver_search_verified(p):
            """네이버 지역검색 상위 10개를 받아, 검색어와 같은 장소를 LLM이 한 번에 고른다.
            고른 후보의 (경도, 위도) 반환. 맞는 게 없으면 None (→ 도로명 폴백).
            (맨 위 1개만 쓰면 엉뚱한 상호가 걸리므로, 10개를 함께 보고 이름이 맞는 걸 선택)"""
            try:
                results = naver_local_search(p, display=10)
            except Exception as e:
                print(f"[NAVER][ERROR] 검색 실패 ({p}): {e}")
                return None
            cands = [r for r in results if r["lon"] is not None]  # 좌표 있는 후보만
            if not cands:
                print(f"[NAVER] 검색 결과 없음: '{p}'")
                return None

            lines = "\n".join(
                f"{i}. {r['title']} / {r['road_address'] or r['address']}"
                for i, r in enumerate(cands, 1)
            )
            print(f"[NAVER] '{p}' 후보 {len(cands)}개:\n{lines}")
            verdict = llm([{"role": "user",
                            "content": prompts.pick_geocode_match_prompt(p, lines)}]).strip()

            m = re.search(r"\d+", verdict)
            if not m:
                print(f"[NAVER] '{p}' → 일치 후보 없음(도로명 폴백) [응답: {verdict}]")
                return None
            idx = int(m.group()) - 1
            if not (0 <= idx < len(cands)):
                print(f"[NAVER] '{p}' → 번호 범위 밖(도로명 폴백) [응답: {verdict}]")
                return None
            pick = cands[idx]
            print(f"[NAVER] '{p}' → 선택: {pick['title']} (위도 {pick['lat']}, 경도 {pick['lon']})")
            return pick["lon"], pick["lat"]

        def geocode_place(place_text):
            """(경도, 위도) 반환. 못 찾으면 (None, None) → payload에 빈칸(null).
            위치 텍스트는 키워드 추출 LLM이 이미 지역명까지 붙인 완전한 지명이므로 그대로 검색.
              1) 장소이름 그대로 지역검색 → 결과 이름 O/X 검증
              2) 검증 실패/무결과 시 RAG 매칭 도로명으로 지오코딩"""
            p = (place_text or "").strip()
            if not p or p in ("미언급", "모름"):
                return None, None

            # 1) 지역검색 + 이름 일치 검증
            hit = naver_search_verified(p)
            if hit:
                return hit
            # 2) 도로명 지오코딩
            #    이 단계는 예전에 아무 로그도 남기지 않아서, 지역검색이 0건일 때
            #    "폴백이 돌기는 했나"를 알 수 없었다. RAG 가 찾은 도로명을 찍어둔다.
            road = road_address_for(p)
            if not road:
                print(f"[GEOCODE] 도로명 폴백 불가: '{p}' — RAG place_matches 에 "
                      f"이 지명의 도로명이 없다 (좌표 빈칸으로 진행)")
                return None, None
            print(f"[GEOCODE] 도로명 폴백: '{p}' → {road}")
            lon, lat = geocode_address(road)
            if lon or lat:
                return lon, lat
            return None, None  # 좌표 없음 → 빈칸

        rep_lon, rep_lat = geocode_place(reporter_text)
        odor_lon, odor_lat = geocode_place(odor_text)
        # 지오코딩은 네이버/NCP 왕복 + LLM 후보 선택이 섞여 있어 의외로 오래 걸릴 수 있다.
        timer.mark("좌표 변환(네이버/NCP)")

        # 좌표를 대시보드에도 올린다 → 키워드 카드에서 지도로 보여준다.
        # 텍스트 결과는 위 그래프 루프의 마지막 write_progress 가 이미 보냈고, 지오코딩은
        # 그 뒤에 끝나므로 좌표만 덧붙인 스냅샷을 한 번 더 쓴다. 지도는 부가 기능이라
        # 여기서 실패해도 파이프라인(서버 B 전송·저장)은 그대로 진행시킨다.
        try:
            write_progress(build_snapshot(
                NODE_STEP.get("extract_keywords", 8), _disp(result), file=fname, job=job_token,
                extra={
                    "rep_lon": rep_lon, "rep_lat": rep_lat, "rep_name": reporter_text,
                    "odor_lon": odor_lon, "odor_lat": odor_lat, "odor_name": odor_text,
                }))
        except Exception as e:
            print(f"[MAP][WARN] 좌표 스냅샷 기록 실패: {e}")

        # 4) 학습용 원천 데이터 저장
        try:
            save_dir = save_raw_dataset(job, stt_text, normalized_text, parsed)
            print(f"[DATASET] Saved raw data to {save_dir}")
        except Exception as e:
            print(f"[DATASET][ERROR] Failed to save raw data: {e}")
        timer.mark("원천 데이터 저장")

        # 5) 서버 B로 전송
        send_json = {
            "full_text": normalized_text,          # 보정+표준어 변환된 최종 텍스트
            "stt_text": stt_text,                  # (참고용) STT 원문
            "location": reporter_text,             # 신고자 위치 (원문, 미언급이어도 항상 전송)
            "region": result.get("estimated_region", ""),  # 권역 (사투리·지명 기반 추정)
            "odor_type": parsed["odor_type"],
            "suspected_source": odor_text,         # 냄새 위치 (원문, 미언급이어도 항상 전송)
            "intensity_change": parsed["intensity_change"],
            "duration": parsed["duration"],
            "contact": DEFAULT_CONTACT,
            "longitude": rep_lon,        # 신고자 위치 좌표 (없으면 null)
            "latitude": rep_lat,
            "odor_longitude": odor_lon,  # 악취 위치 좌표 (없으면 null)
            "odor_latitude": odor_lat,
        }
        try:
            send_result_to_server_b(send_json)
            print("[SERVER A] Result sent to Server B")
        except Exception as e:
            print(f"[SERVER A][ERROR] Failed to send to Server B: {e}")
        timer.mark("서버 B 전송")

        # 마지막 줄: 단계별 + 전체 처리시간. 한 통화의 결산이다.
        timer.report(fname)

    set_processor(process_fn)
    start_worker(num_threads=1)

    yield

    stop_worker()
