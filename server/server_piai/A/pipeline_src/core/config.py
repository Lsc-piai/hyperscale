"""
config.py
- 전역 설정 및 공유 객체 초기화
- LLM 클라이언트 (vLLM, 8001): Qwen3.6-27B 메인 모델 (main_client)
- LLM 클라이언트 (vLLM, 8002): Qwen3-0.6B 파인튜닝 키워드 모델 (kw_client, KEYWORDS_VERSION=1일 때만)
  (8000/8100은 서버 A/B가 사용하므로 vLLM은 8001/8002로 배치)
- STT: Whisper large-v3 로컬 transformers 파이프라인 (get_whisper_pipeline)
  vLLM의 /v1/audio/transcriptions는 30초 초과 오디오를 자체 청크 분할하는데,
  분할 파라미터가 모델 클래스에 하드코딩돼 있어 조정이 불가능하고 청크 경계에서
  구간이 통째로 누락되는 현상이 있어 transformers pipeline(청크+오버랩 스티칭)으로 대체.
- 임베딩 모델 (ko-sroberta-multitask): 지명 FAISS 검색용 로컬 로드 (get_embedder)
- 무거운 모델(whisper/embedder)은 import 시점이 아닌 호출 시점에 로드하고 캐시하는
  lazy 싱글턴 getter다 (경로 상수만 쓰는 모듈이 import했다가 GPU 메모리를 잡는 부작용
  방지). 서버 A는 lifespan startup에서 이 getter를 한 번 호출해 미리 로드해둔다
  (app/core/lifespan.py) — 그래도 이 모듈 자체가 import 시 로드하진 않는다.
- 경로 상수: FAISS 인덱스 디렉토리, 자모 분해 테이블
"""
import os
from pathlib import Path
from openai import OpenAI

# 배포 환경에 따라 환경변수로 오버라이드 가능:
#   MAIN_LLM_URL / KW_LLM_URL : vLLM 서버 주소
#   LLM_API_KEY               : vLLM 을 `--api-key` 로 띄웠을 때 그 키.
#     예전에는 실키가 여기 하드코딩돼 있었다 — 이 파일을 볼 수 있는 누구나 우리 27B 를
#     쓸 수 있었다는 뜻이다. 이제 환경변수로만 받는다. run_server.py 가 넣어준다.
#     env 가 없으면 "EMPTY"(vLLM 관례)를 보낸다 → --api-key 를 쓴 서버면 401 이 나서
#     설정 누락이 조용히 넘어가지 않는다.
#   DATASET_DIR               : dataset 폴더 절대경로 (voice_saturi, faiss 포함)
_LLM_API_KEY = os.environ.get("LLM_API_KEY") or "EMPTY"
main_client = OpenAI(base_url=os.environ.get("MAIN_LLM_URL", "http://localhost:8001/v1"), api_key=_LLM_API_KEY)
kw_client = OpenAI(base_url=os.environ.get("KW_LLM_URL", "http://localhost:8002/v1"), api_key=_LLM_API_KEY)

# A 루트 (= A/pipeline_src/core/config.py 기준 세 단계 위 = server/A)
_A_ROOT = Path(__file__).resolve().parents[2]

_WHISPER_MODEL_ID = os.environ.get("WHISPER_MODEL_ID", "openai/whisper-large-v3")
# 지명 검색용 임베딩 모델 (ko-sroberta-multitask). 기본값은 A/models 안 (자립형).
_MODEL_PATH = Path(os.environ.get(
    "EMBED_MODEL_PATH",
    str(_A_ROOT / "models" / "ko-sroberta-multitask"),
))

_whisper_pipeline = None
_embedder = None


def get_whisper_pipeline():
    """Whisper large-v3 ASR 파이프라인 (첫 호출 시 GPU 로드).
    chunk_length_s/stride_length_s 방식은 transformers가 seq2seq(Whisper)에는 "매우 실험적"이라고
    명시 경고하는 방식이라 쓰지 않는다. 대신 모델 자체 내장 long-form generate 메커니즘을 쓰도록
    chunk_length_s를 지정하지 않고, 호출 시 return_timestamps=True로 위임한다.
    WHISPER_DEVICE 로 올릴 GPU 를 바꿀 수 있다(기본 cuda:0)."""
    global _whisper_pipeline
    if _whisper_pipeline is None:
        import torch
        from transformers import pipeline as _hf_pipeline
        device = os.environ.get("WHISPER_DEVICE", "cuda:0")
        _whisper_pipeline = _hf_pipeline(
            "automatic-speech-recognition",
            model=_WHISPER_MODEL_ID,
            torch_dtype=torch.float16,
            device=device,
        )
    return _whisper_pipeline


def get_embedder():
    """ko-sroberta-multitask 임베딩 모델 (첫 호출 시 로드).
    EMBED_DEVICE 로 올릴 GPU 를 바꿀 수 있다(기본 cuda:0)."""
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        device = os.environ.get("EMBED_DEVICE", "cuda:0")
        _embedder = SentenceTransformer(str(_MODEL_PATH), device=device)
    return _embedder


# FAISS DB 등이 있는 dataset 폴더. 기본값은 A/dataset 안 (자립형).
DATASET_DIR = Path(os.environ.get("DATASET_DIR", str(_A_ROOT / "dataset")))
FAISS_DIR       = DATASET_DIR / "faiss" / "location"      # 지명 검색 인덱스 (search.py)
STT_ERR_FAISS   = DATASET_DIR / "faiss" / "stt_err"       # 오인식-정답 few-shot (search.py)

REGIONS = ["경상도", "전라도", "충청도", "강원도", "제주도"]

# 광역 지역 판별 시 LLM에게 참고 자료로 주는 지역별 대표 종결어미
REGION_ENDINGS = {
    "경상도": ["~노", "~제", "~라예", "~심더", "~니더", "~기라"],
    "전라도": ["~당께", "~잉", "~부러", "~소", "~겠소", "~잖소"],
    "충청도": ["~유", "~슈", "~겨"],
    "강원도": ["~드래요", "~래요"],
    "제주도": ["~마씸", "~수다", "~우다"],
}

CHOSUNG  = ['ㄱ','ㄲ','ㄴ','ㄷ','ㄸ','ㄹ','ㅁ','ㅂ','ㅃ','ㅅ','ㅆ','ㅇ','ㅈ','ㅉ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ']
JUNGSUNG = ['ㅏ','ㅐ','ㅑ','ㅒ','ㅓ','ㅔ','ㅕ','ㅖ','ㅗ','ㅘ','ㅙ','ㅚ','ㅛ','ㅜ','ㅝ','ㅞ','ㅟ','ㅠ','ㅡ','ㅢ','ㅣ']
JONGSUNG = ['','ㄱ','ㄲ','ㄳ','ㄴ','ㄵ','ㄶ','ㄷ','ㄹ','ㄺ','ㄻ','ㄼ','ㄽ','ㄾ','ㄿ','ㅀ','ㅁ','ㅂ','ㅄ','ㅅ','ㅆ','ㅇ','ㅈ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ']
