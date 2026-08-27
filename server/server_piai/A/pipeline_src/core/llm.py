"""
llm.py
- 메인 LLM 호출 래퍼 (Qwen3.6-27B): 지역 추정·지명 보정·숫자 변환·오인식 보정·표준어 변환 등 (llm)
- kw_llm: 파인튜닝 키워드 추출 모델 (Qwen3-0.6B)
- _vad_chunks: Silero VAD(모델 기반 음성 구간 감지) 기반 오디오 분할 — STT 청킹용
  (app/core/stt.py 와 공유). 목소리/잡음을 구분해 조용한 발화 누락·잡음 예민 문제를 줄임.
"""
import os

from core.config import main_client, kw_client

# 메인 LLM 모델명 — vLLM 이 서빙하는 이름과 정확히 같아야 한다(--served-model-name 또는 HF 경로).
# 예) FP8 실험: MAIN_LLM_MODEL="Qwen/Qwen3.8-27B-FP8" python run_a.py
MAIN_LLM_MODEL = os.environ.get("MAIN_LLM_MODEL", "Qwen/Qwen3.6-27B")

# Silero VAD 모델은 1회 로드 후 재사용 (lazy)
_silero_model = None
_silero_utils = None


def _get_silero():
    global _silero_model, _silero_utils
    if _silero_model is None:
        from silero_vad import load_silero_vad, get_speech_timestamps
        _silero_model = load_silero_vad()
        _silero_utils = get_speech_timestamps
    return _silero_model, _silero_utils


def llm(messages: list, max_tokens: int = 2048, temperature: float = 0.3) -> str:
    # temperature: 기본 0.3(화자분리·환각제거·오인식보정·표준어변환 등 표현 유연성 있는 단계).
    #   정답이 정해진 단계(시군/광역 추정, 지명복원 pick_best·숫자변환, 키워드 추출)는
    #   호출 시 temperature=0(greedy)으로 넘겨 결정적·재현 가능하게 한다.
    response = main_client.chat.completions.create(
        model=MAIN_LLM_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=0.8,
        extra_body={
            "top_k": 20,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    content = response.choices[0].message.content
    return content.strip() if content else ""


def kw_llm(prompt: str) -> str:
    response = kw_client.chat.completions.create(
        model="kw_client",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=256,
        temperature=0.01,
        top_p=0.95,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return response.choices[0].message.content.strip()


def _speech_intervals(audio, sr: int):
    """Silero VAD로 음성 구간 [(start, end), ...](샘플 단위) 반환 (목소리/잡음 구분)."""
    import torch
    model, get_speech_timestamps = _get_silero()
    ts = get_speech_timestamps(
        torch.as_tensor(audio, dtype=torch.float32), model,
        # speech_pad_ms=30: 패딩이 크면(150) 인접 청크 경계가 겹쳐 같은 발화가 중복 전사되는
        # 문제가 있어 whisper 폴더 실험 결과에 맞춰 30으로 축소(중복↓). 첫/끝 음절 잘림은 미미.
        sampling_rate=sr, speech_pad_ms=30,
    )
    return [(t["start"], t["end"]) for t in ts]


def _vad_chunks(audio, sr: int, max_chunk: float = 30.0):
    """Silero VAD 음성 구간을 인접한 것끼리 max_chunk초까지 묶어 디코딩 조각으로 반환.
    (whisper 폴더 120버전 전수 실험 — CER/WER·환각률·키워드보존율·LLM 정밀 누락감사 전부
    비교한 결과: pad30 + merge + chunk30 조합이 전 지표에서 최선으로 확정됨. 과거엔 '병합하면
    Whisper가 발화를 통째로 스킵한다'고 봤으나, 그건 병합 윈도우에 길이 제한이 없던 구식 구현
    문제였고, 지금처럼 max_chunk(=Whisper 30초 한계 이내)로 캡을 씌운 병합은 그 문제가 없고
    오히려 문맥이 늘어 CER·키워드보존율·환각률이 전부 개선됨을 800건 규모로 확인했다.)"""
    chunks = []
    cur = None
    for s, e in _speech_intervals(audio, sr):
        if cur is None:
            cur = [s, e]
        elif (e - cur[0]) / sr <= max_chunk:  # 다음 구간까지 합쳐도 한도 이내면 병합
            cur[1] = e
        else:
            chunks.append(cur)
            cur = [s, e]
    if cur is not None:
        chunks.append(cur)

    out = []
    for s, e in chunks:
        while (e - s) / sr > max_chunk:  # 단일 장발화가 한도를 넘으면 강제 분할
            cut = s + int(max_chunk * sr)
            out.append([s, cut])
            s = cut
        out.append([s, e])
    return out
