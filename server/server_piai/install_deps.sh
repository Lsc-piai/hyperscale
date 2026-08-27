#!/usr/bin/env bash
# install_deps.sh — 서버 A/B/UI 실행에 필요한 패키지 설치 (도커 커밋 전에 1회)
#
#   bash install_deps.sh
#
# 왜 스크립트인가: pip 를 여러 번 나눠 부르면 resolver 가 그때그때 다른 결정을 해서
# 기존 스택을 건드릴 수 있다. **한 번에 묶어서** 설치하고, 끝나면 vLLM 이 여전히
# import 되는지까지 확인해야 한다. 그 순서를 고정해 둔 것이다.
#
# 이 스크립트가 하는 일
#   ① 현재 패키지 목록을 백업 (문제 생기면 되돌릴 근거)
#   ② ffmpeg 바이너리 설치 (decoder.py 가 subprocess 로 직접 부른다)
#   ③ requirements.txt 를 constraints.txt 와 함께 설치
#   ④ 전 모듈 import 검증 + pip check
#
# 도커 커밋과의 관계
#   /root/project 는 ceph 마운트라 **이미지에 안 담긴다.** 하지만 conda 환경
#   (/root/miniconda3)과 apt 로 깐 ffmpeg 는 마운트 밖이라 이미지에 남는다.
#   즉 이 스크립트의 결과물은 커밋으로 보존된다 — 그게 커밋하는 이유다.

set -u   # -e 는 쓰지 않는다: 검증 단계에서 실패한 항목을 **전부** 보여주고 싶다

PY=/root/miniconda3/envs/server/bin/python
PIP=/root/miniconda3/envs/server/bin/pip
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$DIR/pip-freeze-before-$STAMP.txt"

echo "══ ① 설치 전 패키지 목록 백업 ══"
"$PIP" freeze > "$BACKUP" && echo "  → $BACKUP ($(wc -l < "$BACKUP") 줄)"

echo
echo "══ ② ffmpeg 바이너리 ══"
if command -v ffmpeg >/dev/null 2>&1; then
    echo "  이미 있음: $(ffmpeg -version 2>/dev/null | head -1)"
else
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq ffmpeg
    command -v ffmpeg >/dev/null 2>&1 \
        && echo "  설치됨: $(ffmpeg -version | head -1)" \
        || echo "  ✗ 설치 실패 — 업로드된 오디오를 디코딩할 수 없다 (A 가 파일을 못 읽는다)"
fi

echo
echo "══ ③ pip 설치 (constraints 로 기존 스택 고정) ══"
# -c constraints.txt 가 핵심이다. 이게 없으면 sentence-transformers 를 맞추려고
# transformers 를 내려서 vLLM 을 깨뜨릴 수 있다.
"$PIP" install -r "$DIR/requirements.txt" -c "$DIR/constraints.txt"
PIP_RC=$?
echo "  pip 종료코드: $PIP_RC"

echo
echo "══ ④ 검증 ══"
"$PY" - <<'PYEOF'
import importlib, sys
# (모듈명, 무엇이 죽는지) — 실패했을 때 영향 범위를 바로 알 수 있게
targets = [
    ("vllm",                  "vLLM 27B (이게 깨지면 설치가 기존 스택을 건드렸다)"),
    ("torch",                 "전체"),
    ("transformers",          "Whisper STT"),
    ("fastapi",               "서버 A·B"),
    ("uvicorn",               "서버 A·B"),
    ("langgraph",             "서버 A 파이프라인"),
    ("sqlalchemy",            "서버 B DB"),
    ("tenacity",              "서버 B 웹훅 재시도"),
    ("gradio",                "진행상황 UI"),
    ("sentence_transformers", "임베딩 검색"),
    ("faiss",                 "지명·오인식 인덱스"),
    ("scipy",                 "지명 검색"),
    ("silero_vad",            "발화 구간 분할(VAD)"),
    ("ffmpeg",                "오디오 디코딩"),
]
bad = []
for mod, impact in targets:
    try:
        m = importlib.import_module(mod)
        v = getattr(m, "__version__", "")
        print(f"  OK   {mod:22s} {v}")
    except Exception as e:
        bad.append((mod, impact, e))
        print(f"  ✗    {mod:22s} {type(e).__name__}: {e}")
if bad:
    print("\n  실패한 것:")
    for mod, impact, e in bad:
        print(f"    {mod} → '{impact}' 가 동작하지 않는다")
    sys.exit(1)
print("\n  전부 import 됨")
PYEOF
IMPORT_RC=$?

echo
echo "══ 의존성 충돌 검사 (pip check) ══"
"$PIP" check || echo "  ↑ 충돌이 보이면 위 백업 파일과 비교할 것: $BACKUP"

echo
if [ "$PIP_RC" -eq 0 ] && [ "$IMPORT_RC" -eq 0 ]; then
    echo "✅ 설치 완료. 다음 단계:"
    echo "   1) 실제 기동 확인:  python run_server.py --skip-vllm"
    echo "   2) 문제 없으면 도커 커밋"
    echo "   3) 배포 UI 실행 파일: $PY $DIR/run_server.py"
else
    echo "⚠  실패한 항목이 있다. 되돌리려면:"
    echo "   $PIP install -r $BACKUP"
fi
