# run_b.py  (서버 B)
# 웹훅 사용 시 실행 전 환경변수 설정 필요 ("Webhook 참고.txt" 참고):
#   export PARTNER_WEBHOOK_URL=...
#   export PARTNER_WEBHOOK_TOKEN=...
# 개발 클러스터 배치: B = 로그인 노드 8100 (A는 n1:8000, qwen vLLM은 n1:8001).
#   로그인 노드 8000은 타 사용자 점유라 8100 사용. 실서버(전용 B 머신)면 SERVER_B_PORT=8000.
import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("run_b:app", host="0.0.0.0", port=int(os.environ.get("SERVER_B_PORT", "8100")))
