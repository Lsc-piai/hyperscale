# run_a.py  (서버 A)
# 실행 전 vLLM 서버가 떠 있어야 함:
#   port 8001: Qwen/Qwen3.6-27B (메인 LLM)
#   port 8002: 파인튜닝 Qwen3-0.6B (--served-model-name kw_client, KEYWORDS_VERSION=1 사용 시)
# 파이프라인 코드는 A 안(A/pipeline_src)에 포함 — raw 폴더 불필요.
# A 밖 자원만 env로 지정(배포 시 필수): EMBED_MODEL_PATH(ko-sroberta), DATASET_DIR(FAISS DB)
from app.core.config import SERVER_A_PORT  # import 시 A/pipeline_src를 sys.path에 주입
from app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("run_a:app", host="0.0.0.0", port=SERVER_A_PORT, reload=False)
