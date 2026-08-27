# app/core/stt.py  (서버 A)
"""
업로드 오디오(numpy) → Whisper large-v3 STT
- 디코딩된 numpy 배열을 입력받아 Silero VAD(모델 기반) 청킹 후 조각별로 전사한다.
- VAD 청킹 사용 이유: 감정/템포 증강이 심한 사투리 발화에서 Whisper 내장 long-form
  디코딩이 구간을 통째로 누락하는 문제가 있어, 음성 경계로 잘라 조각별 단일 윈도우
  디코딩한다. (_vad_chunks 는 파이프라인 core.llm 와 공유)
- 모델 로드/캐싱은 get_whisper_pipeline()이 담당 (서버 기동 시 lifespan에서 미리 로드해둠)
"""
import os
import re
import time

from app.core.config import PIPELINE_DIR  # noqa: F401  (sys.path 주입 보장용)
from core.config import get_whisper_pipeline
from core.llm import _vad_chunks

# 세그먼트 디코딩이 저신뢰(logprob/압축률 기준 미달)일 때 그 구간을 버리지 않고 온도를 높여
# 재시도하도록 온도 폴백을 준다. 사투리·긴 이름에서 구간이 통째로 누락되는 문제를 크게 줄인다.
_GEN_KW = {
    "language": "korean",
    "task": "transcribe",
    "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
}

# 판정 기준 단계적 완화.
# whisper 온도 폴백은 temperature만 단계별로 바꾸고 logprob/no_speech 기준은 스칼라로 고정한다.
# 그래서 바깥에 단계 루프를 하나 더 둔다: 1차(엄격) 기준으로 전사했는데 구간이 통째로 비면
# (저신뢰로 누락된 것) 기준을 완화해 재전사하고, 텍스트가 나오면 그 단계 결과를 채택한다.
#   logprob_threshold  ↓ (덜 엄격) : 낮은 신뢰도 전사도 받아들임
#   no_speech_threshold ↑ (덜 엄격) : 무음으로 판정해 버리는 경우를 줄임
_THRESHOLD_STAGES = [
    {"logprob_threshold": -1.0, "no_speech_threshold": 0.6},   # 1차: 기본
    {"logprob_threshold": -1.5, "no_speech_threshold": 0.7},   # 2차: 살짝 완화
    {"logprob_threshold": -2.0, "no_speech_threshold": 0.8},   # 3차: 더 완화
    {"logprob_threshold": -2.5, "no_speech_threshold": 0.9},   # 4차: 최대 완화
]

# VAD 청크 최대 길이(초) = 병합(_vad_chunks) 윈도우 한도이자 강제분할 기준.
# whisper 폴더에서 (진폭/신경망 VAD × 병합/비병합 × pad 30~150ms × chunk 10~30s) 완전 요인설계
# 120버전을 노이즈(통화열화) 800건 전량으로 비교(CER/WER·환각률·키워드보존율·LLM 정밀 누락감사)한
# 결과, "신경망 VAD + 병합 + chunk 30초"가 전 지표 최고였다(CER 6.3%, nomerge 대비도 우위,
# 키워드보존율도 더 높고 누락도 늘지 않음 — 과거의 "병합=스킵" 우려는 병합 윈도우에 길이
# 제한이 없던 구식 구현 문제였고, max_chunk로 캡을 씌운 지금 방식엔 해당 없음).
_MAX_CHUNK = 30.0

# 영어 환각 재시도 (self-consistency).
# 저신뢰 청크는 온도 폴백에서 temperature>0으로 샘플링되므로 실행마다 결과가 다르다.
# 이때 한국어 대신 영어가 통째로 튀어나오는 '나쁜 draw'가 나올 수 있는데,
# 그런 청크만 다시 전사해(주사위 다시 굴리기) 정상 결과를 얻는다.
# 정상 청크는 재시도하지 않으므로 속도에 영향이 없다.
_MAX_RERUN = 3  # 영어 과다 시 최대 재시도 횟수. 소진해도 계속 영어면 그 청크는 버린다.

# ── 부분 누락(청크의 일부 구간만 전사됨) 감지 → 쪼개서 재전사 ────────────────
# 실측 사례(경상도_id243, 82.9s / 13턴):
#   VAD 청크 3 = 60.35~82.93s 안에 t11·t12·t13 세 발화가 들어 있었는데, Whisper 가
#   마지막 t13(45자)만 내놓고 앞의 16초(t11·t12)를 통째로 건너뛰었다. 오디오·VAD·업로드는
#   모두 정상이었다(원본 TTS wav 와 바이트 동일, VAD 가 81.3/82.9초를 덮음).
# 기존 방어는 결과가 **완전히 빈** 청크만 재시도했으므로 이런 '일부만 나온' 청크는 그냥 통과했다.
# 판정: 청크 길이 대비 글자수(자/초)가 비정상적으로 낮으면 앞이나 뒤가 날아간 것으로 본다.
#   같은 파일 실측 — 정상 청크 ≈ 7.5자/초, 누락된 청크 = 2.0자/초.
# 복구: 조각을 조용한 지점에서 반으로 쪼개 각각 다시 전사하고, 합친 결과가 더 길면 채택한다.
#   (윈도우가 짧아지면 이 누락이 거의 사라진다. 의심 청크에만 걸리므로 평소 속도엔 영향 없음)
_MIN_CPS = 3.0        # 이 미만이면 부분 누락 의심 (자/초)
_SPLIT_MIN_SEC = 8.0  # 이보다 짧은 청크는 쪼개지 않는다 (짧으면 원래 자/초가 요동친다)
_MAX_SPLIT_DEPTH = 2  # 30s → 15s → 7.5s 까지만


def _is_english_heavy(text: str) -> bool:
    """청크 전사 결과가 영어 환각인지 판정. 라틴 문자가 15자 이상이면서
    한글 문자 수의 0.5배를 넘으면 환각으로 본다(정상 한국어엔 걸리지 않는 임계)."""
    lat = len(re.findall(r'[A-Za-z]', text))
    ko  = len(re.findall(r'[가-힣]', text))
    return lat >= 15 and lat > ko * 0.5


def _transcribe_with_relaxation(pipe, audio, sr, long_form: bool) -> str:
    """온도 폴백 + 판정기준 단계적 완화로 한 구간(또는 전체)을 전사.
    각 단계는 온도 폴백을 그대로 태우되 logprob/no_speech 기준만 완화하며,
    비어있지 않은 텍스트가 처음 나오는 단계의 결과를 채택한다."""
    extra = {"return_timestamps": True} if long_form else {}
    text = ""
    for i, stage in enumerate(_THRESHOLD_STAGES):
        result = pipe(
            {"array": audio, "sampling_rate": sr},
            generate_kwargs={**_GEN_KW, **stage},
            **extra,
        )
        text = result["text"].strip()
        if text:
            if i > 0:
                print(f"[STT] 판정기준 {i + 1}차 완화로 구간 복원 ({stage})")
            return text
    return text  # 모든 단계에서 빈 결과면 마지막(빈) 값


def _quiet_split(seg, sr: int) -> int:
    """조각을 반으로 자를 지점(샘플 인덱스). 가운데 ±25% 안에서 **가장 조용한 100ms**를 고른다.
    말 중간을 자르면 그 낱말이 양쪽에서 다 사라질 수 있어 무음 지점을 찾는다."""
    import numpy as np
    n = len(seg)
    win = max(1, int(0.1 * sr))
    lo, hi = int(n * 0.25), max(int(n * 0.75) - win, int(n * 0.25) + 1)
    step = max(1, win // 2)
    energies = [(float(np.abs(seg[p:p + win]).mean()), p) for p in range(lo, hi, step)]
    if not energies:
        return n // 2
    return min(energies)[1] + win // 2


def _transcribe_chunk(pipe, seg, sr: int, depth: int = 0) -> tuple:
    """청크 한 개 전사 + 적응 로직(완화 재전사 + 영어환각 재전사 + 부분누락 분할 재전사).
    (텍스트, 상태) 를 돌려준다. 상태: "ok" | "empty"(누락) | "english"(버림) | "split"(분할 복구).
    로깅은 호출부에서 청크 순서대로 하도록 여기선 판정만 한다(스레드에서 병렬 실행되므로)."""
    t = _transcribe_with_relaxation(pipe, seg, sr, long_form=False)
    # 영어 환각이면 최대 _MAX_RERUN번 재전사(온도 폴백 샘플링이라 매번 다른 결과가 나올 수 있다).
    tries = 0
    while t and _is_english_heavy(t) and tries < _MAX_RERUN:
        t = _transcribe_with_relaxation(pipe, seg, sr, long_form=False)
        tries += 1
    if t and _is_english_heavy(t):  # 재시도 소진 후에도 영어면 잡음 구간으로 보고 버린다
        return "", "english"

    # 부분 누락 감지 → 조용한 지점에서 반으로 쪼개 각각 재전사 (사유는 위 상수 주석)
    dur = len(seg) / sr
    if depth < _MAX_SPLIT_DEPTH and dur >= _SPLIT_MIN_SEC and len(t) < dur * _MIN_CPS:
        cut = _quiet_split(seg, sr)
        a, _sa = _transcribe_chunk(pipe, seg[:cut], sr, depth + 1)
        b, _sb = _transcribe_chunk(pipe, seg[cut:], sr, depth + 1)
        merged = " ".join(x for x in (a, b) if x).strip()
        if len(merged) > len(t):
            print(f"[STT] 부분누락 의심 ({dur:.1f}s 에 {len(t)}자 = "
                  f"{len(t) / dur:.1f}자/초) → 반으로 쪼개 재전사: {len(t)}자 → {len(merged)}자",
                  flush=True)
            return merged, "split"
    return t, ("empty" if not t else "ok")


def transcribe_numpy(audio_np, sampling_rate: int = 16000, on_progress=None) -> str:
    """float32 모노 16kHz 배열 → 한국어 전사 텍스트 (VAD 청킹, 조각별 순차 전사).

    on_progress(partial_text): 청크가 하나 끝날 때마다 '지금까지 누적된 전사 텍스트'로
    호출된다(있으면). 화면에 STT 를 실시간으로 흘려보내는 용도. 예외는 삼켜서 전사 자체는
    이 콜백 때문에 멈추지 않게 한다.
    """
    audio = audio_np.astype("float32")
    sr = sampling_rate
    pipe = get_whisper_pipeline()

    def _emit(texts):
        if on_progress is None:
            return
        try:
            on_progress(" ".join(t for t in texts if t))
        except Exception as e:
            print(f"[STT][WARN] 진행 콜백 실패(무시): {e}")

    chunks = _vad_chunks(audio, sr, max_chunk=_MAX_CHUNK)
    if not chunks:  # 음성 미검출 시 기존 long-form 방식으로 폴백
        return _transcribe_with_relaxation(pipe, audio, sr, long_form=True)

    print(f"[STT] Silero VAD: {len(chunks)}개 청크 (최대 {_MAX_CHUNK:.0f}s)")
    # pad=0: Silero가 이미 speech_pad_ms 여유를 줬으므로 추가 패딩을 두지 않아 경계 중복을 막는다.
    pad = 0
    t0 = time.perf_counter()
    texts = []
    covered = 0
    for i, (s, e) in enumerate(chunks, 1):
        seg = audio[max(0, s - pad):min(len(audio), e + pad)]
        dur = (e - s) / sr
        covered += dur
        t, status = _transcribe_chunk(pipe, seg, sr)
        if status == "english":  # 재시도 소진 후에도 영어면 잡음 구간으로 보고 버린다
            print(f"[STT] 청크 {i}/{len(chunks)} 영어 환각 {_MAX_RERUN}회 재시도 실패 → 버림")
            continue
        if status == "empty":  # 저신뢰로 빈 결과 → 그 구간 누락(진단용 로그)
            print(f"[STT] 청크 {i}/{len(chunks)} ({dur:.1f}s) 빈 결과 → 누락")
        # 청크별 진단: 구간·길이·글자수·자당초. 누락이 의심될 때 '어느 구간이 얼마나 빠졌나'를
        # 로그만 보고 바로 알 수 있어야 한다(예전엔 총 청크 수만 찍혀서 추적이 불가능했다).
        print(f"[STT] 청크 {i}/{len(chunks)} {s / sr:6.1f}~{e / sr:6.1f}s ({dur:4.1f}s) "
              f"→ {len(t):4d}자 ({len(t) / dur if dur else 0:.1f}자/초)"
              f"{' [분할복구]' if status == 'split' else ''}", flush=True)
        texts.append(t)
        _emit(texts)                      # 청크마다 누적 텍스트를 화면으로 흘려보낸다
    elapsed = (time.perf_counter() - t0) * 1000
    total = len(audio) / sr
    print(f"[STT-TIME] 전사: 청크 {len(chunks)}개 / {elapsed:.0f} ms "
          f"(오디오 {total:.1f}s 중 VAD 가 덮은 구간 {covered:.1f}s)", flush=True)
    return " ".join(t for t in texts if t)
