# app/core/config.py  (서버 A)
"""
서버 A 설정
- 처리 파이프라인(사투리 STT 보정)은 A 안에 자립형으로 포함돼 있다: A/pipeline_src/
  (raw/whisper/main 에서 복사한 core/pipeline/evaluation 패키지. raw 폴더에 의존하지 않음)
- 모델/데이터도 A 안에 있다: A/models(ko-sroberta, Qwen3-0.6B-full), A/dataset(FAISS DB, voice_saturi)
  다른 위치로 배포할 때만 env로 오버라이드하면 된다:
    EMBED_MODEL_PATH : ko-sroberta 임베딩 모델 경로 (기본값 A/models/ko-sroberta-multitask)
    DATASET_DIR      : FAISS DB 등이 있는 dataset 폴더 (기본값 A/dataset)
  (둘 다 A/pipeline_src/core/config.py 가 읽음)
- 포트 배치 (개발 클러스터): A·B 모두 n1 안에서 실행.
    A    = n1:8000        (음성 처리)
    qwen = n1:8001        (27B vLLM)
    B    = n1:8100        (저장·조회)
  A→B 전송은 같은 n1 안이라 localhost:8100.
  외부 접근은 로그인 노드의 리버스 프록시(scripts/proxy.py, :8000)가
  경로로 분기: /upload_audio→A, /complaints→B.
"""
import os
import sys
from pathlib import Path

# vLLM 서버 주소 (파이프라인 core/config.py가 이 환경변수를 읽는다)
#   메인 LLM(Qwen3.6-27B) = 8001, 키워드 LLM(0.6B) = 8002
#   ※ 파이프라인 import 이전에 설정돼야 하므로 여기(가장 먼저 import되는 모듈)에서 지정.
#     setdefault라 사용자가 직접 export한 값이 있으면 그쪽이 우선한다.
os.environ.setdefault("MAIN_LLM_URL", "http://localhost:8001/v1")
os.environ.setdefault("KW_LLM_URL", "http://localhost:8002/v1")

# A 루트 (= app/core/config.py 기준 두 단계 위)
A_ROOT = Path(__file__).resolve().parents[2]

# 자립형 파이프라인 소스 (A 안). core/pipeline/evaluation 이 top-level로 import되도록 path에 추가.
PIPELINE_DIR = A_ROOT / "pipeline_src"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

# ── 서버 A 자체 설정 ─────────────────────────────────────────
SERVER_A_PORT = int(os.environ.get("SERVER_A_PORT", "8000"))

# 서버 B (민원 저장/플랫폼 API) — A와 B가 같은 n1 안에서 실행되므로 localhost:8100.
#   (외부 접근은 로그인 노드의 리버스 프록시(scripts/proxy.py)가 담당)
SERVER_B_URL = os.environ.get(
    "SERVER_B_URL", "http://127.0.0.1:8100/internal/complaints"
)

# 키워드 추출 방식: 1 = 파인튜닝 Qwen3-0.6B(kw_llm, port 8002), 2 = Qwen3.6-27B 프롬프트(8001)
#   기본 2: qwen(27B, 8001) 한 대만 띄우는 배치라 별도 0.6B 서버 불필요.
#   0.6B를 쓰려면 8002에 kw_client 띄우고 KEYWORDS_VERSION=1.
KEYWORDS_VERSION = int(os.environ.get("KEYWORDS_VERSION", "2"))

# 학습용 원천 데이터(오디오/STT/키워드) 저장 위치 (A 안, 기본값)
BASE_SAVE_DATA_DIR = os.environ.get(
    "BASE_SAVE_DATA_DIR", str(A_ROOT / "data" / "complaints_raw")
)

# 민원인 연락처 기본값 (녹취 서버에서 안 넘어오는 경우)
DEFAULT_CONTACT = "010-0000-0000"
