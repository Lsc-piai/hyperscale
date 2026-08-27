#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_server.py — 외부 서버(piai / Kong) 배포용 **단일 엔트리포인트**

배포 UI 에 이 파일 하나만 지정하면 된다. 수동 준비 단계가 없다:

    python /root/project/server/server_piai/run_server.py

  이 파일이 혼자서 다 한다.
    ① 비밀값 4개를 만들거나 읽어온다 (source 할 쉘 스크립트가 필요 없다)
    ② vLLM(27B) 을 띄운다
    ③ 서버 A(음성 처리) 를 띄운다
    ④ 서버 B(저장·조회·웹훅) 를 띄운다
    ⑤ 진행상황 UI 를 띄운다
    ⑥ 인증 게이트웨이를 **이 프로세스 안의 스레드로** 돌린다
    ⑦ 외부 접속 주소와 토큰을 로그에 찍는다
    ⑧ SIGTERM(배포 UI 의 '정지') 을 받으면 자식들을 순서대로 정리한다

  cwd 와 무관하게 동작한다 (경로는 전부 이 파일 위치 기준). 어느 폴더에서 실행해도 된다.

═══════════════════════════════════════════════════════════════════════════════
■ 내부 서버에서 하던 5단계와의 대응

    내부 (터미널 4개 + 로그인 노드)                여기 (이 파일 하나)
    ─────────────────────────────────────────────────────────────────────────
    vllm serve Qwen3.8-27B-FP8  (n1:8001)          ② --api-key 붙여서
    POOL_STRATEGY=... python run_a.py              ③ POD_IP 에만 바인딩
    웹훅 env + python run_b.py                     ④ 웹훅 env 는 아래에서 설정
    HOST=... python app_ui_live.py                 ⑤ Basic 인증 강제
    로그인 노드에서 proxy.py --allow-*             ⑥ IP 허용목록 → 토큰 인증

■ 바인딩 주소가 곧 방화벽이다 (이 배치의 핵심)

  이 pod 의 code-server 는 `--auth none` 으로 떠 있고 내장 프록시(`/proxy/<포트>/`)가
  살아있다. 즉 **127.0.0.1 에 listen 하는 모든 포트가 무인증으로 외부에 열린다.**
  A 의 /upload_audio, B 의 /internal/complaints, vLLM 27B 까지 전부.

  그 프록시는 `127.0.0.1:<포트>` 로만 연결한다. 그래서 앱을 POD IP 에만 바인딩하면
  프록시가 닿지 못한다(실측: 500). 이게 유일한 봉쇄 수단이다.

      외부 ─Kong─▶ code-server /proxy/8000/ ─▶ 127.0.0.1:8000  게이트웨이(이 프로세스)
                                                     │ 토큰 검사
                          ┌──────────────────────────┼─────────────────────┐
                          ▼                          ▼                     ▼
                  POD_IP:8000 A             POD_IP:8100 B          POD_IP:7860 UI
                          └──▶ POD_IP:8001 vLLM

  게이트웨이와 A 가 똑같이 8000 을 써도 인터페이스가 달라 충돌하지 않는다.

  ※ run_a.py / run_b.py 는 host="0.0.0.0" 이 하드코딩돼 있다. 0.0.0.0 은 127.0.0.1 을
    포함하므로 그대로 띄우면 위 봉쇄가 무너진다. 그래서 두 파일을 고치는 대신
    uvicorn 에 --host 를 준다 (두 파일 모두 `app` 을 모듈 전역에 두므로 import 된다).

■ 비밀값 4개 — 배포 UI 에서 env 로 주거나, 안 주면 자동 생성된다

    GATEWAY_TOKEN         /upload_audio·/complaints 접근 (유일한 방어선)
    LLM_API_KEY           vLLM --api-key ↔ A
    LIVE_PROGRESS_TOKEN   A 의 /live_progress (민원 전사문·주소)
    UI_PASS               /ui 화면 (브라우저는 헤더를 못 붙여 토큰을 못 쓴다)

  env 로 주지 않으면 SECRETS_FILE(기본 kong/.secrets.env)에 난수로 만들어 저장하고
  다음 기동부터 그 파일을 읽는다 — 재기동해도 토큰이 바뀌지 않는다(바뀌면 녹취
  클라이언트·파트너 설정이 매번 깨진다). 값은 기동 로그에 찍히므로 배포 UI 의 로그에서
  확인해 클라이언트에 전달하면 된다.

  ★ GATEWAY_TOKEN 이 유일한 방어선인 이유: A 의 /upload_audio 와 B 의 /complaints 에는
    인증 코드가 전혀 없다. 내부 서버에서는 리버스 프록시의 IP 허용목록이 그 역할을
    했지만, Kong 뒤에서는 모든 요청이 게이트웨이 IP 로 도착해 IP 로 구분이 불가능하다.

■ 파트너 웹훅 (B → 파트너). 배포 UI 의 환경변수로 주면 된다

    PARTNER_WEBHOOK_URL    비우면 B 는 웹훅 전송만 건너뛴다 (나머지는 정상)
    PARTNER_WEBHOOK_TOKEN

  ※ 이 pod 은 밖으로 나가는 통신(egress)이 막혀 있을 수 있다. 파트너 주소로 실제로
    나갈 수 있는지 먼저 확인할 것 — kong/webhook_viewer.py 의 POD 모드로 검증한다.

■ 자주 쓰는 옵션 (배포 UI 에 인자를 넣을 수 있으면)

    --skip-vllm     vLLM 은 따로 띄워둔 경우
    --skip-ui       API 만
    --only gateway  하나만 (디버깅)
    --print-secrets no   비밀값을 로그에 찍지 않는다 (로그가 공용일 때)

■ 남는 위험 (알고 쓸 것)
  · TLS 가 없다. 토큰과 UI 비밀번호가 평문으로 지나간다.
  · pod 이 재생성되면 URL 의 JF_POD_NAME 해시가 바뀐다 → 파트너에게 준 주소가 죽는다.
  · code-server 웹 IDE 자체는 여전히 무인증으로 열려 있다. 이 코드로는 못 막는다.
═══════════════════════════════════════════════════════════════════════════════
"""
import argparse
import datetime
import http.client
import os
import re
import secrets as _secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── 경로 (cwd 와 무관하게 이 파일 위치 기준) ──────────────────────────────
PIAI_DIR = Path(__file__).resolve().parent        # server_piai/
SERVER_DIR = PIAI_DIR.parent                      # server/
A_DIR = PIAI_DIR / "A"
B_DIR = PIAI_DIR / "B"
UI_DIR = A_DIR / "ui"
VLLM_SERVE = SERVER_DIR / "vllm" / "serve.py"
KONG_DIR = PIAI_DIR / "kong"
LOG_DIR = KONG_DIR / "logs"

# 없으면 난수로 만들어 채우는 값.
# ── BLAS/OpenMP 스레드 상한 (서버 A 전용) ──────────────────────────────────
#
# ■ 왜 필요한가 — 실제로 A 가 SIGSEGV 로 죽었다 (2026-08-20, exit=-11)
#   FAISS RAG 검색(경상도 70만건) 중에 이 메시지가 수백 줄 찍히고 프로세스가 터졌다:
#       BLAS : Program is Terminated. Because you tried to allocate too many memory regions.
#
#   이 노드는 **CPU 224 코어**인데, numpy/scipy 가 쓰는 OpenBLAS 는
#   **MAX_THREADS=64 로 빌드**돼 있다(scipy-openblas64 0.3.30, DYNAMIC_ARCH NO_AFFINITY).
#   스레드 수 상한을 주지 않으면 OpenBLAS 가 코어 수만큼 스레드를 만들려 하고,
#   컴파일 시 정해진 버퍼 개수를 넘겨 위 오류 → 세그폴트가 된다.
#   (faiss 는 search.py 가 이미 omp_set_num_threads(4) 로 묶어 뒀지만, numpy/scipy
#    경로는 그 설정과 무관하다 — 그래서 여기서 env 로 막는다)
#
# ■ 반드시 **자식 프로세스 env** 로 줘야 한다
#   numpy/OpenBLAS 는 import 시점에 스레드 풀을 정한다. 부모가 나중에 바꿔도 안 먹는다.
#
# ■ 값 선택: 16. OpenBLAS 상한(64)에 한참 못 미치므로 안전하고, 코어를 16개까지는
#   쓰므로 검색 속도도 유지된다. 느리면 BLAS_THREADS 로 올려볼 수 있다(64 미만 유지).
# 자식이 죽었을 때 되살릴 최대 횟수. 0 이면 예전처럼 전체를 내린다.
RESTART_MAX = int(os.environ.get("RESTART_MAX", "5"))

BLAS_THREADS = os.environ.get("BLAS_THREADS", "16")
BLAS_ENV = {
    "OMP_NUM_THREADS": BLAS_THREADS,
    "OPENBLAS_NUM_THREADS": BLAS_THREADS,
    "MKL_NUM_THREADS": BLAS_THREADS,
    "NUMEXPR_NUM_THREADS": BLAS_THREADS,
    "VECLIB_MAXIMUM_THREADS": BLAS_THREADS,
}

SECRET_KEYS = ("UI_USER", "UI_PASS")

# 있으면 그 기능을 켜고, 없으면 끄고 진행하는 값. 난수로 만들지 않는다.
# 값은 배포 UI 의 환경변수로 넣는다.
#
#   ■ 원래부터 여기 있던 것 — 외부에서 발급받은 키·주소
#     NAVER_*/NCP_* : 예전엔 A/app/core/naver_api.py 와 A/ui/app_ui_live.py 에 실키가
#                     하드코딩돼 있었다. 파일을 볼 수 있는 누구나 우리 할당량을 썼다.
#     PARTNER_*     : B → 파트너 웹훅. B/Webhook 참고.txt 에 평문으로 적혀 있던 것.
CONFIG_KEYS = ("GATEWAY_TOKEN", "LLM_API_KEY", "LIVE_PROGRESS_TOKEN",
               "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET",
               "NCP_GEOCODE_KEY_ID", "NCP_GEOCODE_KEY",
               "PARTNER_WEBHOOK_URL", "PARTNER_WEBHOOK_TOKEN")

ALL_KEYS = SECRET_KEYS + CONFIG_KEYS

# 이게 없으면 그 기능이 조용히 꺼진다 — 기동 로그에서 알려주기 위한 묶음.
_FEATURE_OF = {
    ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"): "장소 이름 검색(네이버 지역검색)",
    ("NCP_GEOCODE_KEY_ID", "NCP_GEOCODE_KEY"): "주소→좌표 변환(NCP 지오코딩) · UI 지도",
    ("PARTNER_WEBHOOK_URL",): "파트너 웹훅 전송",
}


def log(msg):
    print(f"[run_server] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
#  1. 환경 — POD IP, 포트, 외부 주소, 비밀값
# ══════════════════════════════════════════════════════════════════════════

# 자식(A·B·UI)을 띄울 파이썬 인터프리터 후보. 순서대로 시도한다.
#   ★ sys.executable 을 쓰면 안 된다. 배포 UI 는 이 파일을 **시스템 파이썬**
#     (/usr/bin/python3)으로 실행하고, 거기엔 uvicorn·gradio 가 없다. 그래서
#     "No module named uvicorn" 으로 A·B 가 1초 만에 죽는다(실측).
#     패키지는 conda 환경(/root/miniconda3/envs/server)에만 있다 — 도커 이미지에
#     담긴 것도 그쪽이다. serve.py 가 vLLM 바이너리를 절대경로로 부르는 것과 같은 이유.
PYTHON_CANDIDATES = (
    os.environ.get("PYTHON"),                        # 명시 지정이 최우선
    "/root/miniconda3/envs/server/bin/python",       # 실제 패키지가 있는 곳
    sys.executable,                                  # 마지막 수단
)

# 자식이 반드시 import 할 수 있어야 하는 모듈. 하나라도 없으면 그 인터프리터는 탈락.
_CHILD_NEEDS = ("uvicorn", "fastapi")


def child_python():
    """A·B·UI 를 띄울 파이썬을 고른다. 후보를 실제로 import 검사해서 정한다.

    기동 직후에 한 번 걸러내는 이유: 인터프리터가 틀리면 자식이 1초 만에 죽는데,
    그걸 세 번 반복하고 나서야 알게 되면 로그만 지저분해진다. 여기서 한 줄로 끝낸다."""
    tried = []
    for cand in PYTHON_CANDIDATES:
        if not cand or not Path(cand).exists():
            continue
        try:
            r = subprocess.run([cand, "-c", "import " + ", ".join(_CHILD_NEEDS)],
                               capture_output=True, timeout=60)
            if r.returncode == 0:
                if tried:
                    log(f"파이썬 선택: {cand} (앞선 후보 탈락: {', '.join(tried)})")
                else:
                    log(f"파이썬: {cand}")
                return cand
            tried.append(f"{cand}({', '.join(_CHILD_NEEDS)} 없음)")
        except (OSError, subprocess.TimeoutExpired) as e:
            tried.append(f"{cand}({type(e).__name__})")
    raise SystemExit(
        f"[run_server] 중단: {', '.join(_CHILD_NEEDS)} 를 import 할 수 있는 파이썬이 없다.\n"
        f"  시도: {', '.join(tried) or '(후보 없음)'}\n"
        f"  PYTHON=<파이썬 절대경로> 로 지정하거나, install_deps.sh 로 패키지를 설치할 것.")


def pod_ip():
    """pod eth0 IP. A/B/UI/vLLM 은 여기에만 바인딩된다 → code-server 프록시가
    (127.0.0.1 로만 연결하므로) 닿지 못해 외부에서 안 보인다."""
    if os.environ.get("POD_IP"):
        return os.environ["POD_IP"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect 는 패킷을 보내지 않는다. 라우팅 테이블만 물어보는 용도.
        # 대상 주소는 파드에 항상 주입되는 환경변수에서 받는다 (값을 코드에 박지 않는다).
        s.connect((os.environ["KUBERNETES_SERVICE_HOST"], 443))
        return s.getsockname()[0]
    except (OSError, KeyError):
        return "127.0.0.1"
    finally:
        s.close()


def gpu_available(py):
    """CUDA GPU 를 **실제로** 쓸 수 있는지 자식 프로세스에서 확인한다.

    ★ py 는 child_python() 이 고른 파이썬이어야 한다. sys.executable 을 쓰면 안 된다 —
      배포 UI 가 이 파일을 시스템 파이썬으로 실행하면 그쪽에는 torch 가 없어서,
      GPU 가 멀쩡한데도 "GPU 없음"으로 오판하고 vLLM·A 를 건너뛴다. 실제로 그렇게
      오진했다: 진단에는 /dev/nvidia0·libcuda·nvidia-smi 가 다 정상인데
      `ModuleNotFoundError: No module named 'torch'` 가 찍혔다.

    왜 env 로 판단하지 않나: JF_NUM_GPUS·CUDA_VISIBLE_DEVICES 는 '할당 요청'을 말할 뿐
    디바이스가 정말 붙어 있는지는 보장하지 않는다. 실제로 배포 pod 에서 JF_NUM_GPUS=1
    인데 CUDA 디바이스가 없어서, vLLM 과 서버 A 가 각각 5번씩 재시작하며 같은
    트레이스백(`Failed to infer device type` / `No CUDA GPUs are available`)을 쏟아냈다.
    원인은 로그 저 아래 묻히고, 정작 필요한 한 줄("GPU 가 없다")은 아무도 말해주지 않았다.

    왜 자식 프로세스인가: 이 함수는 게이트웨이가 도는 부모에서 불린다. 부모에서 torch 를
    import 해 CUDA 를 초기화하면 컨텍스트가 VRAM 을 잡고, 곧 뜨는 vLLM 자식과 겹친다.
    """
    code = ("import torch, sys; "
            "sys.exit(0 if torch.cuda.is_available() and torch.cuda.device_count() > 0 else 9)")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, timeout=180)
        return r.returncode == 0
    except Exception as e:
        log(f"[WARN] GPU 점검 자체가 실패했다 ({type(e).__name__}: {e}) → 없다고 본다")
        return False


def gpu_diagnostics(py):
    """GPU 점검이 실패했을 때 **원인을 특정할 수 있는 사실만** 모아 로그로 찍는다.

    왜 필요한가: 배포 UI 에서 GPU 를 1장 할당했는데도 torch 가 못 보는 경우가 있다.
    원인이 셋인데 대처가 전혀 다르다.

      ① /dev/nvidia* 가 없다            → GPU 가 파드에 안 붙었다. 배포 설정 문제.
      ② /dev/nvidia* 는 있고 libcuda 없다 → 장치는 붙었는데 **드라이버 라이브러리가
                                          미마운트**. 플랫폼(nvidia device plugin) 문제.
                                          `Triton ... 0 active driver(s) found (expected 1)`
                                          이 그 신호다 — 장치 수는 세는데 드라이버가 없다.
      ③ 둘 다 있는데 torch 가 실패        → 드라이버/CUDA 런타임 버전 불일치.

    배포 pod 에는 셸로 들어갈 수 없으므로, 이 정보가 로그에 남지 않으면 ①②③을
    구분할 방법이 없다. 정상 파드 기준값: /dev/nvidia0 존재,
    libcuda.so.1 → /usr/lib/x86_64-linux-gnu/, LD_LIBRARY_PATH 에 /usr/local/nvidia/lib64.
    """
    log("── GPU 진단 ──────────────────────────────────────────────────────")

    # ① 장치 파일
    devs = sorted(p.name for p in Path("/dev").glob("nvidia*"))
    log(f"  /dev/nvidia*        : {', '.join(devs) if devs else '없음'}")

    # ② 드라이버 라이브러리
    lib = None
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=20)
        for ln in out.stdout.splitlines():
            if "libcuda.so.1" in ln and "=>" in ln:
                lib = ln.split("=>")[-1].strip()
                break
    except Exception:
        pass
    if not lib:
        # ldconfig 캐시에 없어도 LD_LIBRARY_PATH 경로에 파일이 있을 수 있다
        for d in os.environ.get("LD_LIBRARY_PATH", "").split(":"):
            if d and (Path(d) / "libcuda.so.1").exists():
                lib = str(Path(d) / "libcuda.so.1") + " (LD_LIBRARY_PATH)"
                break
    log(f"  libcuda.so.1        : {lib or '없음  ← 드라이버 라이브러리 미마운트'}")

    # ③ nvidia-smi
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=30)
        smi = (r.stdout or r.stderr).strip().splitlines()
        log(f"  nvidia-smi -L       : {smi[0] if smi else '(출력 없음)'}")
        for extra in smi[1:4]:
            log(f"                        {extra}")
    except FileNotFoundError:
        log("  nvidia-smi -L       : 명령 자체가 없다")
    except Exception as e:
        log(f"  nvidia-smi -L       : 실패 ({type(e).__name__}: {e})")

    # ④ 관련 환경변수
    for k in ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES",
              "NVIDIA_DRIVER_CAPABILITIES", "JF_NUM_GPUS", "LD_LIBRARY_PATH"):
        log(f"  {k:20s}: {os.environ.get(k, '<미설정>')}")

    # ⑤ torch 가 내는 실제 오류 (자식에서 — 부모에 CUDA 컨텍스트를 만들지 않는다)
    code = ("import torch;"
            "print('torch', torch.__version__, '· 빌드 CUDA', torch.version.cuda);"
            "print('device_count', torch.cuda.device_count());"
            "\ntry:\n import torch;torch.cuda.init();print('init OK')\n"
            "except Exception as e:\n print('init 실패:', type(e).__name__, e)")
    torch_missing = False
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=180)
        out = r.stdout + r.stderr
        torch_missing = "No module named 'torch'" in out
        log(f"  검사에 쓴 파이썬     : {py}")
        for ln in out.strip().splitlines()[:6]:
            log(f"  torch               : {ln}")
    except Exception as e:
        log(f"  torch               : 점검 실패 ({type(e).__name__}: {e})")

    # ⑥ 해석 — 위 사실을 조합해 어디에 문의할지 알려준다
    if torch_missing:
        log("  ▶ 판정: **GPU 문제가 아니다.** 장치·드라이버는 정상인데 이 파이썬에")
        log(f"         torch 가 없다 → {py}")
        log("         run_server.py 를 torch 가 있는 파이썬으로 실행해야 한다:")
        log("           /root/miniconda3/envs/server/bin/python .../run_server.py")
        log("         또는 PYTHON=<절대경로> env 로 지정할 것.")
    elif not devs:
        log("  ▶ 판정: GPU 장치가 파드에 없다. **배포 설정에서 GPU 할당**을 확인할 것.")
        log("         (배포 UI 에 '1장'으로 보여도 실제 파드에 안 붙는 경우가 있다)")
    elif not lib:
        log("  ▶ 판정: 장치는 붙었는데 **드라이버 라이브러리(libcuda.so.1)가 없다.**")
        log("         우리가 고칠 수 없다 — 플랫폼(조나단) 담당자에게 문의할 내용:")
        log("         '배포 파드에 GPU 장치는 마운트됐는데 NVIDIA 드라이버 라이브러리가")
        log("          주입되지 않습니다. 개발 파드에서는 libcuda.so.1 이")
        log("          /usr/lib/x86_64-linux-gnu 에 있고 LD_LIBRARY_PATH 에")
        log("          /usr/local/nvidia/lib64 가 들어 있습니다.'")
    else:
        log("  ▶ 판정: 장치·드라이버는 있는데 torch 가 초기화에 실패했다.")
        log("         위 'torch init 실패' 줄이 원인이다 (드라이버/런타임 버전 불일치 등)")
    log("──────────────────────────────────────────────────────────────────")


def load_or_create_secrets(path: Path, generate: bool):
    """비밀값·외부키를 env → 파일 → (비밀값만) 새로 생성 순서로 확보한다.

    파일 형식은 `export K='V'` 로 쓴다. 쉘에서 그대로 source 되면 kong/webhook_viewer.py
    같은 도구를 손으로 띄울 때 편하다.

    우선순위가 env → 파일인 이유: 배포 UI 의 환경변수가 더 안전한 보관처다(pod 이
    지워져도 남는다). 파일은 그게 없을 때의 대비책이다."""
    got = {k: os.environ[k] for k in ALL_KEYS if os.environ.get(k)}
    from_env = set(got)

    if path.exists():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*(?:export\s+)?([A-Z_]+)\s*=\s*'([^']*)'\s*$", text, re.M):
            k, v = m.group(1), m.group(2)
            if k in ALL_KEYS and k not in got and v:   # env 로 준 값이 항상 이긴다
                got[k] = v

    got.setdefault("UI_USER", "piai")
    missing = [k for k in SECRET_KEYS if not got.get(k)]

    if missing:
        if not generate:
            raise SystemExit(
                f"[run_server] 중단: 비밀값이 없다 → {', '.join(missing)}\n"
                f"  배포 UI 의 환경변수로 주거나, --generate-secrets 로 자동 생성할 것.")
        log(f"비밀값 생성: {', '.join(missing)}")
        lengths = {"UI_PASS": 12}
        for k in missing:
            got[k] = _secrets.token_urlsafe(lengths.get(k, 24))

    # 새로 만든 게 있으면 파일에 남긴다 (재기동해도 같은 값이 되도록).
    # 이미 있던 값은 got 에 들어 있으므로 그대로 다시 쓴다 — 덮어써서 잃지 않는다.
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            "# 자동 생성/갱신 — run_server.py. 절대 git 에 올리지 말 것.",
            "# 쉘에서 `source` 하면 그대로 환경변수가 된다.",
            "",
            "# ── 우리가 정하는 비밀값 (지우고 재기동하면 새 값으로 재생성) ──────────",
        ]
        body += [f"export {k}='{got.get(k, '')}'" for k in SECRET_KEYS]
        body += [
            "",
            "# ── 외부에서 발급받은 키·주소 (난수로 만들 수 없다. 비면 그 기능만 꺼진다) ──",
            "# 더 안전한 보관처는 배포 UI 의 환경변수다 — 이 파일은 pod 이 지워지면 사라진다.",
        ]
        body += [f"export {k}='{got.get(k, '')}'" for k in CONFIG_KEYS]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        log(f"비밀값 저장: {path} (권한 600)")

    if from_env:
        log(f"배포 UI env 에서 온 값: {', '.join(sorted(from_env))}")

    # 외부 키가 비어 있으면 어떤 기능이 꺼지는지 알려준다. 조용히 좌표만 안 붙는 상태가
    # 제일 찾기 어렵다 — 파이프라인은 정상으로 보이는데 결과에 좌표가 없다.
    any_absent = False
    for keys, feature in _FEATURE_OF.items():
        absent = [k for k in keys if not got.get(k)]
        if absent:
            any_absent = True
            log(f"[WARN] {', '.join(absent)} 없음 → '{feature}' 꺼진다")

    if any_absent:
        # 배포 UI 목록에는 분명히 있는데 프로세스는 못 보는 경우가 있었다
        # (NCP_GEOCODE_KEY 는 NCP_GEOCODE_KEY_ID 의 접두사다 — 한쪽이 덮이거나
        #  이름에 공백이 붙으면 이름만 봐서는 구분이 안 된다).
        # 그래서 프로세스가 **실제로 보는 이름**을 그대로 찍는다. repr 로 찍어야
        # 'NCP_GEOCODE_KEY ' 처럼 뒤에 공백이 붙은 이름이 눈에 보인다.
        # 값은 길이만 — 로그에 비밀이 남지 않게.
        hits = sorted(k for k in os.environ
                      if any(t in k.upper() for t in ("NCP", "NAVER", "GEOCODE",
                                                      "WEBHOOK", "GATEWAY", "LLM_API")))
        log("── 프로세스가 보는 관련 env 이름 (값은 길이만) ──")
        for k in hits:
            log(f"   {k!r} = <{len(os.environ[k])}자>")
        if not hits:
            log("   (하나도 없다 → 배포 UI 의 env 가 프로세스에 전달되지 않는 상태)")
        log("──────────────────────────────────────────────")

    # 넣었으면 최소 길이를 지킬 것.
    gt = got.get("GATEWAY_TOKEN", "")
    if gt and len(gt) < 16:
        raise SystemExit(f"[run_server] 중단: GATEWAY_TOKEN 이 너무 짧다({len(gt)}자). "
                         f"32자 이상으로 하거나, 아예 비워서 검사를 끌 것.")
    return got


def detect_mode():
    """이 프로세스가 IDE 파드에서 도는가, 배포(deployment)로 도는가.

    ★ 이 판별이 **바인딩 주소를 뒤집는다.** 트래픽이 pod 에 도달하는 경로가 다르기 때문이다.

      IDE 파드   Kong → code-server(pod 안) → 127.0.0.1:<포트>
                 code-server 가 루프백으로 접속한다. 그래서 게이트웨이만 127.0.0.1 에 두고
                 A/B/UI/vLLM 은 POD_IP 에 둬야 외부에 안 보인다.
                 (URL: .../vscode/<POD>-<IDX>/proxy/<포트>/  ← 포트를 URL 로 고른다)

      배포       Kong → k8s Service → <POD_IP>:<포트>
                 서비스는 파드 IP 로 접속한다. 그래서 **정반대**로, 게이트웨이를 0.0.0.0 에
                 두고 A/B/UI/vLLM 을 127.0.0.1 에 둬야 한다. 배포에서는 루프백이 진짜로
                 사설이다 — 프록시가 없으니 아무도 못 닿는다.
                 (URL: .../deployment/<HASH>/  ← 포트 구간이 없다 = 포트 하나만 열린다)

      IDE 배치를 배포에 그대로 올리면 게이트웨이가 127.0.0.1 에만 있어서 **외부에서
      아무것도 안 열린다** (서비스가 파드 IP 로 왔는데 거기엔 아무도 없다).

    판별 근거: VSCODE_PROXY_URI 는 code-server 가 있는 파드에만 있다.
    """
    if os.environ.get("RUN_MODE") in ("ide", "deployment"):
        return os.environ["RUN_MODE"]
    return "ide" if os.environ.get("VSCODE_PROXY_URI") else "deployment"


def external_base_url(gw_port):
    """외부 진입 주소를 런타임에 계산한다. **배포마다 달라지므로 하드코딩하면 안 된다.**

    Kong 이 code-server 로 보내고, code-server 가 이 접두사를 떼고 127.0.0.1:<gw_port>
    로 넘긴다. 주소를 알아내는 방법을 정확한 순서로 시도한다:

      ① EXTERNAL_BASE_URL          — 사람이 직접 지정한 값. 항상 최우선.
      ② VSCODE_PROXY_URI           — **플랫폼이 스스로 알려주는 템플릿.** 가장 믿을 만하다.
                                     예: http://<IP>:<PORT>/vscode/<POD>-<IDX>/proxy/{{port}}/
                                     `{{port}}` 를 게이트웨이 포트로 바꿔 쓴다.
      ③ JF_* 조각으로 조립         — ②가 없을 때만. 아래 주의사항 참고.

    ②를 ③보다 먼저 쓰는 이유: ③은 인그레스 주소·포트·경로 조각을 **우리가 추측해서**
    조립한다. 새 배포에서 인그레스 IP·포트가 바뀌거나
    서비스 이름이 vscode 가 아니면 조용히 틀린 주소를 만들어낸다 — "주소는 찍혔는데
    접속이 안 된다"가 되어 원인을 찾기 어렵다. ②는 플랫폼이 준 사실이다.

    ★ 세 방법 모두 실패하면 빈 문자열을 준다. 그 경우 UI 는 뜨지 않는다(root_path 를
      만들 수 없어서). API 는 게이트웨이만 있으면 동작하므로, 주소를 모르는 것과
      서비스가 죽는 것은 다르다.
    """
    if os.environ.get("EXTERNAL_BASE_URL"):
        return os.environ["EXTERNAL_BASE_URL"].rstrip("/")

    tmpl = os.environ.get("VSCODE_PROXY_URI", "")
    if "{{port}}" in tmpl:
        return tmpl.replace("{{port}}", str(gw_port)).rstrip("/")
    if tmpl:
        log(f"[WARN] VSCODE_PROXY_URI 에 '{{{{port}}}}' 자리가 없다 ({tmpl}) → JF_* 로 조립한다")

    # ★ 하드코딩하지 않는다 (위 도크스트링 첫 줄 참고) — 없으면 주소를 모르는 것으로 본다.
    host = os.environ.get("JF_PUBLIC_HOST", "")
    port = os.environ.get("JF_PUBLIC_PORT", "")
    pod = os.environ.get("JF_POD_NAME")
    idx = os.environ.get("JF_POD_INDEX")
    if detect_mode() == "deployment":
        # 배포 주소는 .../deployment/<HASH>/ 형태다. HASH 를 파드 안에서 알아낼 방법이
        # 없으므로(JF_POD_NAME 과 다른 값이다) 반드시 사람이 넣어줘야 한다.
        log("[WARN] 배포 모드인데 EXTERNAL_BASE_URL 이 없다 → 외부 주소를 알 수 없다.")
        log("[WARN]   배포 UI 환경변수에 넣을 것 (끝의 / 는 빼고):")
        log("[WARN]   EXTERNAL_BASE_URL=http://<인그레스호스트>:<포트>/deployment/<배포HASH>")
        log("[WARN]   없으면 API 는 동작하지만 /ui 화면은 안 뜬다 (root_path 를 못 만든다).")
        return ""
    if not host or not port or not pod or idx is None:
        log("[WARN] VSCODE_PROXY_URI 도 JF_POD_NAME 도 없다 → 외부 주소를 알 수 없다. "
            "EXTERNAL_BASE_URL 을 직접 줄 것 (API 는 동작하지만 UI 는 안 뜬다).")
        return ""
    log(f"[WARN] VSCODE_PROXY_URI 가 없어 JF_* 로 조립한다 — 인그레스 주소가 "
        f"{host}:{port}, 경로가 /vscode/ 라고 가정한다. 접속이 안 되면 이 가정을 볼 것.")
    return f"http://{host}:{port}/vscode/{pod}-{idx}/proxy/{gw_port}"


# ══════════════════════════════════════════════════════════════════════════
#  2. 게이트웨이 — 유일한 외부 입구. 이 프로세스 안의 스레드로 돈다.
# ══════════════════════════════════════════════════════════════════════════
#
#  내부 서버의 scripts/proxy.py 를 옮긴 것이다. 달라진 점:
#    · IP 허용목록 → 헤더 검사.
#    · 별도 프로세스 → 스레드. 배포 UI 가 py 파일 하나만 실행하므로 한 파일에 넣었다.

_B_PREFIXES = ("/complaints",)      # B(조회/저장)로. 나머지는 전부 A로.
_UI_PREFIX = "/ui"                  # Gradio. 넘길 때 이 접두사를 뗀다.
_QWEN_PREFIX = "/qwen"
              # vLLM. base_url = <외부주소>/qwen/v1

# 절대 외부로 내보내지 않는 경로.
#   /live_progress      : 민원 전사문·주소를 그대로 돌려준다. UI 는 pod 안에서 읽는다.
#   /internal/*         : A→B 전용. 열리면 외부에서 위조 민원을 넣을 수 있다.
#   /docs,/openapi.json : API 스키마를 통째로 노출할 이유가 없다.
_DENY_PREFIXES = ("/live_progress", "/internal", "/docs", "/redoc", "/openapi.json")

# 프록시가 다시 만들어야 하는 hop-by-hop 헤더 (그대로 넘기면 안 됨)
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

GW = {}                             # 게이트웨이 런타임 설정
_drop_lock = threading.Lock()
_drop_seen = set()


def _record_drop(key, detail):
    """거절된 요청을 기록. (사유, 경로) 조합당 1줄 — 스캐너가 파일을 터뜨리지 않게."""
    path = GW.get("drop_log")
    if not path:
        return
    with _drop_lock:
        if key in _drop_seen:
            return
        _drop_seen.add(key)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write("# 게이트웨이가 거절한 요청 — run_server.py 자동 기록.\n")
                    f.write("# (사유, 경로) 조합당 첫 1줄. 형식(탭 구분): <시각>\t<사유>\t<상세>\n")
                f.write(f"{ts}\t{key}\t{detail}\n")
        except OSError as e:
            log(f"[WARN] drop 로그 기록 실패({path}): {e}")


def token_ok(headers):
    if not GW.get("token"):
        return True          # 토큰 미설정 = 검사 OFF (기본값). 값을 넣으면 자동으로 켜진다
    """공유 비밀 대조. compare_digest 로 타이밍 공격(앞자리부터 맞춰보기)을 막는다."""
    want = GW["token"]
    got = headers.get("X-Gateway-Token", "")
    if got and _secrets.compare_digest(got, want):
        return True
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _secrets.compare_digest(auth[7:], want)
    return False


def route(path):
    """경로 → (이름, 백엔드포트, 벗겨낼 접두사, 토큰 필요 여부)."""
    p = path.split("?", 1)[0]
    if any(p == pre or p.startswith(pre + "/") for pre in _DENY_PREFIXES):
        return "DENY", 0, "", True
    if p == _QWEN_PREFIX or p.startswith(_QWEN_PREFIX + "/"):
        return "QWEN", GW["qwen_port"], _QWEN_PREFIX, True
    if p == _UI_PREFIX or p.startswith(_UI_PREFIX + "/"):
        # 브라우저가 직접 여는 화면이라 헤더를 붙일 수 없다.
        return "UI", GW["ui_port"], _UI_PREFIX, False
    if any(p == pre or p.startswith(pre + "/") for pre in _B_PREFIXES):
        return "B", GW["b_port"], "", True
    return "A", GW["a_port"], "", True


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"        # 응답 후 연결 종료 (단순/안정)

    def log_message(self, fmt, *args):
        cmd = getattr(self, "command", "-")
        path = getattr(self, "path", "-")
        print(f"[gateway] {cmd} {path} -> {args}", flush=True)

    # Gradio 가 `/manifest.json` 을 루트 절대경로로 박아서(root_path 를 안 붙인다)
    # 리로드마다 A 로 새는 404 가 로그를 채운다. 데이터가 없는 요청이라 여기서 끊는다.
    _STATIC_NOOP = ("/manifest.json", "/favicon.ico")

    def _reject(self, code, key, detail):
        """내부 proxy.py 는 소켓을 그냥 끊었다(스캐너에게 존재를 안 알리려고). 여기서는
        상태코드를 준다 — Kong 뒤라 포트 스캔 대상이 아니고, 파트너가 '토큰이 틀렸다'와
        '서버가 죽었다'를 구분 못하면 문의 대응이 불가능하다."""
        print(f"[gateway] REJECT {code} {key} ({detail})", flush=True)
        _record_drop(key, detail)
        body = f"{code} {key}\n".encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _proxy(self):
        if self.path.split("?", 1)[0] in self._STATIC_NOOP:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        target, port, strip, need_token = route(self.path)

        if target == "DENY":
            # 404 로 답한다 — 403 은 "여기 뭔가 있다"를 알려준다.
            self._reject(404, "DENY", f"외부로 내보내지 않는 경로: {self.path}")
            return
        if need_token and not token_ok(self.headers):
            self._reject(401, "NO_TOKEN", f"{target} {self.path} — X-Gateway-Token 없음/불일치")
            return

        # 접두사를 떼고 백엔드로 넘긴다. UI 는 Gradio root_path 가 다시 붙인다
        # (ASGI root_path 규약: 프록시가 벗기고 앱이 붙인다).
        upstream_path = self.path
        if strip:
            upstream_path = self.path[len(strip):]
            if not upstream_path.startswith("/"):
                upstream_path = "/" + upstream_path   # "/ui" → "/", "/ui?x=1" → "/?x=1"

        host = GW["backend_host"]
        try:
            conn = http.client.HTTPConnection(host, port, timeout=GW["timeout"])
            conn.putrequest(self.command, upstream_path, skip_host=True, skip_accept_encoding=True)
            length = None
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in _HOP_BY_HOP:
                    continue
                # 게이트웨이 토큰은 백엔드로 넘기지 않는다 (백엔드가 볼 이유가 없다).
                if lk == "x-gateway-token":
                    continue
                # Authorization 은 /qwen 에서만 의미가 있다 — vLLM 의 --api-key 가 그걸로
                # 인증한다. 그 외 경로에서 Bearer 로 게이트웨이 토큰을 받았다면 우리
                # 인증용이지 백엔드가 볼 것이 아니므로 떼고 보낸다.
                if lk == "authorization" and target != "QWEN" and v.startswith("Bearer "):
                    if _secrets.compare_digest(v[7:], GW["token"]):
                        continue
                if lk == "content-length":
                    length = int(v)
                conn.putheader(k, v)
            conn.endheaders()

            # 요청 본문 스트리밍 (wav 업로드 — 통째로 메모리에 올리지 않는다)
            if length:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    conn.send(chunk)
                    remaining -= len(chunk)

            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(k, v)
            self.end_headers()
            # read1 이어야 한다 — read(65536) 은 **버퍼가 찰 때까지 기다린다.**
            # Gradio 는 진행상황을 SSE 로 흘리고 UI 는 0.1초마다 갱신하므로, 버퍼링되면
            # 화면이 멈춘 것처럼 보인다. read1 은 도착한 만큼만 즉시 돌려준다.
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            conn.close()
        except Exception as e:
            print(f"[gateway] backend error ({target} {host}:{port}): {e}", flush=True)
            try:
                self.send_error(502, "Bad Gateway")
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _proxy


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_gateway(token, backend_host, listen_host, listen_port, a_port, b_port, ui_port, qwen_port):
    """게이트웨이를 스레드로 띄우고 서버 객체를 돌려준다.

    바인딩 주소는 모드에 따라 다르다 (detect_mode 주석 참고):
      IDE 파드 → 127.0.0.1  (code-server 프록시가 루프백으로 온다)
      배포     → 0.0.0.0    (k8s Service 가 파드 IP 로 온다)
    어느 쪽이든 '게이트웨이만 외부에 열리고 뒤의 앱들은 안 보인다'는 성질은 유지된다."""
    GW.update({
        "token": token,
        "backend_host": backend_host,
        "a_port": a_port,
        "b_port": b_port,
        "ui_port": ui_port,
        "qwen_port": qwen_port,
        "timeout": float(os.environ.get("GATEWAY_TIMEOUT", "600")),
        "drop_log": str(KONG_DIR / "rejected.txt"),
    })
    srv = GatewayServer((listen_host, listen_port), GatewayHandler)
    t = threading.Thread(target=srv.serve_forever, name="gateway", daemon=True)
    t.start()
    log(f"게이트웨이 스레드 기동: {listen_host}:{listen_port} → 백엔드 {backend_host}")
    if token:
        log("  인증: X-Gateway-Token 검사 ON")
    else:
        log("  인증: OFF — URL 을 아는 누구나 민원 조회·업로드가 된다 "
            "(GATEWAY_TOKEN 을 넣으면 켜진다)")
    _need = "토큰 필요" if token else "인증 없음"
    log(f"  /upload_audio,/health → A {backend_host}:{a_port}      ({_need})")
    log(f"  /complaints[/last]    → B {backend_host}:{b_port}      ({_need})")
    log(f"  /qwen/v1/...          → vLLM {backend_host}:{qwen_port} ({_need})")
    log(f"  /ui                   → UI {backend_host}:{ui_port}    (토큰 없음, Basic 인증)")
    log(f"  차단 경로: {', '.join(_DENY_PREFIXES)}")
    return srv


# ══════════════════════════════════════════════════════════════════════════
#  3. 자식 프로세스 관리
# ══════════════════════════════════════════════════════════════════════════

# ── 자식 로그를 '부모 stdout'(= 배포 시스템 로그)으로 중계한다 ─────────────
# 배포 UI 는 이 프로세스의 stdout 만 보여준다. kong/logs/a.log 를 열려면 pod 에 들어가야
# 하는데, 배포해 놓고 돌리는 상황에서는 그게 안 된다. 그래서 자식 로그를 부모 stdout 에
# 함께 흘린다 (파일에도 그대로 남는다 — 하트비트의 _last_log_line 이 파일을 읽는다).
#
# 자식별 정책이 다른 이유는 출력량이 3자리수 차이나기 때문이다:
#   "all"  A·B — **전부 올린다.** 이걸 보려고 만든 기능이다. [TIMING] 결산(단계별 +
#          전체 처리시간), [STT] 청크, [NAVER] 후보, [SERVER A] 전송결과가 다 보인다.
#   튜플   UI — Gradio 는 gr.Timer(0.1) 폴링 때문에 uvicorn 접근 로그가 **초당 10줄**
#          쏟아진다. 전부 올리면 A·B 로그가 그 아래로 밀려 안 보인다. 그래서 골라 올린다.
#   None   vLLM — tqdm 진행률 바(\r 로 같은 줄 덮어쓰기)가 쏟아진다. 중계할 줄도 없고,
#          파이프를 거치면 한 줄이 수 KB 로 뭉친다. 파일에 직접 쓰게 두는 게 낫다.
_FORWARD_POLICY = {
    "a": "all",
    "b": "all",
    "ui": ("[app_ui_live]", "WARNING", "ERROR", "CRITICAL", "Traceback", "Exception"),
    "vllm": None,
}

# 전부 보고 싶으면            FORWARD_LOG_PREFIXES=ALL      (UI 접근 로그까지 다 올린다)
# 특정 줄만 보고 싶으면       FORWARD_LOG_PREFIXES="[TIMING],[SERVER A]"
# 자식 로그를 안 보고 싶으면  FORWARD_LOG_PREFIXES=NONE     (파일에만 남는다)
_FORWARD_ENV = os.environ.get("FORWARD_LOG_PREFIXES", "").strip()


def forward_policy(name):
    """자식 이름 → 중계 정책. "all" | 프리픽스 튜플 | None(중계 안 함)."""
    if _FORWARD_ENV:
        up = _FORWARD_ENV.upper()
        if up in ("ALL", "*"):
            return "all"
        if up in ("NONE", "OFF", "0"):
            return None
        return tuple(x.strip() for x in _FORWARD_ENV.split(",") if x.strip())
    return _FORWARD_POLICY.get(name, "all")


class Procs:
    def __init__(self):
        self.items = []
        self.spec = {}          # name → (cmd, cwd, env, 재시작 횟수)
        self.teed = set()       # 로그를 파이프로 읽는(중계하는) 자식 이름

    def spawn(self, name, cmd, cwd, env, _restart=0):
        """자식을 띄운다.

        중계 정책(forward_policy)이 None 이 아니면 stdout 을 파이프로 받아 ① 로그파일에
        그대로 쓰고 ② 정책에 맞는 줄을 부모 stdout(배포 시스템 로그)에도 찍는다.
        None 이면 예전처럼 파일에 직접 쓰게 둔다."""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logfile = LOG_DIR / f"{name}.log"
        # 바이너리로 연다. 텍스트 모드는 universal-newlines 라 \r 을 \n 으로 바꿔버리는데,
        # 그러면 진행률 바가 수천 줄로 불어나고 _last_log_line 의 \r 처리가 무의미해진다.
        # 바이너리로 읽고 쓰면 바이트가 그대로 보존된다.
        f = open(logfile, "ab", buffering=0)
        f.write(f"\n===== {name} 기동 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
                .encode("utf-8"))
        log(f"기동: {name}  (로그: {logfile})")
        # 되살리기용으로 실행 정보를 들고 있는다 (아래 restart()).
        self.spec[name] = (cmd, cwd, env, _restart)
        # start_new_session: 부모가 받은 신호가 자식에게 직접 가지 않게 한다. 우리가
        # 역순으로 정리해야 A→B 전송 중 끊김 같은 지저분한 종료를 줄일 수 있다.
        policy = forward_policy(name)
        if policy is not None:
            p = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            self.teed.add(name)
            threading.Thread(target=self._pump, args=(name, p, f, policy),
                             name=f"pump-{name}", daemon=True).start()
        else:
            self.teed.discard(name)
            p = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=f,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        self.items.append((name, p, f))
        return p

    def _pump(self, name, p, f, policy):
        """자식 stdout → ① 로그파일(전부) ② 부모 stdout(고른 줄만).

        파일 쓰기가 먼저다 — 중계 판정에서 뭐가 잘못되더라도 로그는 온전히 남아야 한다.
        스레드가 죽어도 자식은 계속 돈다(파이프가 차면 자식이 멈추므로, 실패하면
        경고를 남기고 남은 출력을 계속 버려서라도 읽어낸다)."""
        pumping = True
        try:
            for line in p.stdout:                      # 바이너리, \n 단위
                if pumping:
                    try:
                        f.write(line)
                    except (ValueError, OSError) as e:  # 파일이 이미 닫혔다(종료 중)
                        pumping = False
                        print(f"[run_server][WARN] {name} 로그 파일 쓰기 중단: {e}", flush=True)
                if policy == "all" and not line.strip():
                    continue                            # 빈 줄은 시스템 로그에 안 올린다
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                stripped = text.lstrip()
                if policy == "all" or any(x in stripped for x in policy):
                    # 어느 자식이 찍은 줄인지 표시한다 (A/B/UI 가 같은 stdout 에 섞인다).
                    print(f"[{name}] {text}", flush=True)
        except Exception as e:
            print(f"[run_server][WARN] {name} 로그 중계 중단: {e} "
                  f"(자식은 계속 돈다. 로그는 {LOG_DIR / (name + '.log')})", flush=True)
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass

    def dead(self):
        return [(n, p.returncode) for n, p, _ in self.items if p.poll() is not None]

    def restart(self, name):
        """죽은 자식 하나만 되살린다. 성공하면 True.

        ■ 왜 전체를 내리지 않는가
          예전에는 자식 하나가 죽으면 프로세스를 끝냈다(플랫폼이 재시작하게). 그런데
          A 가 요청 처리 중 세그폴트로 죽었을 때 **vLLM 까지 같이 내려가서 17분짜리
          모델 로딩을 다시 했다.** 실제 상황(2026-08-20)에서 그 대가가 너무 컸다.
          그래서 죽은 놈만 다시 띄운다. vLLM 은 살아 있으니 A 는 1~2분에 복귀한다.
        """
        spec = self.spec.get(name)
        if not spec:
            return False
        cmd, cwd, env, n = spec
        if n >= RESTART_MAX:
            log(f"[ERROR] {name} 재시작 한도({RESTART_MAX}회) 초과 — 포기한다")
            return False
        # 죽은 항목을 목록에서 뺀다. 파일 핸들은 중계 중이면 펌프 스레드가 닫는다 —
        # 여기서 닫으면 펌프가 마지막 줄을 쓰다가 ValueError 를 맞는다(죽을 때 남기는
        # 트레이스백이 로그에서 잘려나간다). 중계 안 하는 자식만 여기서 닫는다.
        for i, (nm, pr, fh) in enumerate(self.items):
            if nm == name and pr.poll() is not None:
                if nm not in self.teed:
                    try:
                        fh.close()
                    except Exception:
                        pass
                self.items.pop(i)
                break
        log(f"↻ {name} 재시작 ({n + 1}/{RESTART_MAX})")
        self.spawn(name, cmd, cwd, env, _restart=n + 1)
        return True

    def shutdown(self):
        # 띄운 역순으로 내린다 (게이트웨이는 이미 닫혔으므로 새 요청은 안 들어온다).
        for name, p, _ in reversed(self.items):
            if p.poll() is None:
                log(f"종료: {name}")
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    p.terminate()
        deadline = time.time() + 25
        for name, p, f in reversed(self.items):
            try:
                p.wait(timeout=max(0.5, deadline - time.time()))
            except subprocess.TimeoutExpired:
                log(f"[WARN] {name} 가 안 죽는다 → SIGKILL")
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    p.kill()
            # 중계 중인 자식의 로그 파일은 펌프 스레드가 닫는다 (위 restart() 와 같은 이유).
            if name not in self.teed:
                try:
                    f.close()
                except Exception:
                    pass


# 진행 표시를 넣는 이유: vLLM 27B FP8 은 뜨는 데 수 분 걸린다. 예전에는 그 시간 동안
# 아무것도 찍지 않아서 **켜지는 중인지, 멈춘 건지, 죽은 건지 구분이 안 됐다.**
# 그래서 기다리는 동안 (경과/제한 시간)과 자식 로그의 마지막 줄을 주기적으로 찍는다.
HEARTBEAT_SEC = float(os.environ.get("PROGRESS_INTERVAL", "10"))

# 진행률 바(\r 로 같은 줄을 덮어쓰는 것)와 잡음은 걸러야 마지막 '의미 있는 줄'이 보인다.
_NOISE = ("it/s]", "s/it]", "%|")


def _last_log_line(name, limit=140):
    """자식 로그의 마지막 의미 있는 한 줄. 지금 무슨 단계인지 그대로 보여준다."""
    path = LOG_DIR / f"{name}.log"
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    # \r 로 덮어쓴 진행률 바는 마지막 조각만 남기고, 잡음 줄은 건너뛴다.
    lines = [ln.strip() for chunk in tail.splitlines() for ln in chunk.split("\r")]
    for ln in reversed(lines):
        if ln and not any(n in ln for n in _NOISE):
            return ln if len(ln) <= limit else ln[:limit] + "…"
    return ""


def wait_ready(name, probe, timeout, proc=None, what=""):
    """probe() 가 True 를 줄 때까지 기다리며 진행 상황을 찍는다.

    · HEARTBEAT_SEC 마다: 경과/제한 시간 + 자식 로그의 마지막 줄
    · 자식이 죽으면 **즉시 중단**한다 — 예전에는 죽은 뒤에도 제한 시간을 꽉 채워
      기다려서, 로그를 열어보기 전까지 '멈춤'과 '죽음'을 구분할 수 없었다.
    """
    t0 = time.time()
    deadline = t0 + timeout
    last_beat = t0
    last_err = ""
    log(f"⏳ {name} 기동 대기… ({what or '준비 신호'}, 최대 {int(timeout)}초)")
    while time.time() < deadline:
        # ① 자식이 이미 죽었나
        if proc is not None and proc.poll() is not None:
            log(f"❌ {name} 프로세스가 죽었다 (exit={proc.returncode}, "
                f"{int(time.time() - t0)}초 만에)")
            tail = _last_log_line(name)
            if tail:
                log(f"   마지막 로그: {tail}")
            log(f"   전체 로그: tail -50 {LOG_DIR / (name + '.log')}")
            return False
        # ② 준비됐나
        try:
            if probe():
                log(f"✅ {name} 준비 완료 ({int(time.time() - t0)}초)")
                return True
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        # ③ 아직이면 심장박동
        now = time.time()
        if now - last_beat >= HEARTBEAT_SEC:
            last_beat = now
            el, tot = int(now - t0), int(timeout)
            tail = _last_log_line(name)
            log(f"   … {name} {el}s/{tot}s" + (f" · {tail}" if tail else ""))
        time.sleep(1)
    log(f"⚠ {name} 가 {int(timeout)}초 안에 안 떴다 — 아직 기동 중일 수도 있다")
    if last_err:
        log(f"   마지막 오류: {last_err}")
    tail = _last_log_line(name)
    if tail:
        log(f"   마지막 로그: {tail}")
    log(f"   로그 확인: tail -f {LOG_DIR / (name + '.log')}")
    return False


def probe_http(url, headers=None):
    """/health 가 200 이면 준비된 것으로 본다."""
    def _p():
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    return _p


def probe_tcp(host, port):
    """Gradio 는 /health 가 없으므로 포트가 열리는지로만 본다."""
    def _p():
        with socket.socket() as s:
            s.settimeout(2)
            return s.connect_ex((host, port)) == 0
    return _p


# ══════════════════════════════════════════════════════════════════════════
#  4. main
# ══════════════════════════════════════════════════════════════════════════

_stop = threading.Event()


def main():
    ap = argparse.ArgumentParser(
        description="piai/Kong 배포용 단일 엔트리포인트 (vLLM·A·B·UI·게이트웨이)")
    ap.add_argument("--skip-vllm", action="store_true", help="vLLM 은 이미 떠 있다")
    ap.add_argument("--skip-ui", action="store_true", help="진행상황 UI 를 띄우지 않는다")
    ap.add_argument("--only", choices=["vllm", "a", "b", "ui", "gateway"],
                    help="하나만 띄운다 (디버깅용)")
    ap.add_argument("--vllm-timeout", type=float, default=1200.0,
                    help="vLLM 모델 로딩 대기 상한(초). 27B FP8 은 수 분 걸린다")
    ap.add_argument("--secrets-file", default=os.environ.get("SECRETS_FILE",
                                                             str(KONG_DIR / ".secrets.env")),
                    help="비밀값 저장 경로. 재기동 시 여기서 읽어 같은 토큰을 유지한다")
    ap.add_argument("--generate-secrets", choices=["yes", "no"], default="yes",
                    help="env·파일에 없는 비밀값을 난수로 만들지 (기본 yes)")
    ap.add_argument("--print-secrets", choices=["yes", "no"], default="yes",
                    help="비밀값을 기동 로그에 찍을지. 배포 UI 로그가 공용이면 no")
    args = ap.parse_args()

    ip = pod_ip()
    py = child_python()

    # ── 바인딩 주소는 모드가 정한다 (detect_mode() 주석의 그림 참고) ────────────
    #   IDE 파드: 게이트웨이 127.0.0.1  / 백엔드 POD_IP     (code-server 가 루프백으로 옴)
    #   배포    : 게이트웨이 0.0.0.0    / 백엔드 127.0.0.1  (k8s Service 가 파드 IP 로 옴)
    # 뒤집지 않으면 배포에서 외부 접속이 통째로 막힌다 — 서비스가 파드 IP 로 왔는데
    # 게이트웨이가 루프백에만 있으면 아무도 응답하지 않는다.
    mode = detect_mode()
    if mode == "deployment":
        gw_host, backend_host = "0.0.0.0", "127.0.0.1"
    else:
        gw_host, backend_host = "127.0.0.1", ip
    # 게이트웨이가 들을 포트 = **배포 UI 가 외부 트래픽을 보내는 포트**.
    # 이걸 틀리면 Kong 이 아무도 없는 포트로 연결해서 502 가 난다.
    #   GATEWAY_PORT  : 명시적 지정 (최우선)
    #   PORT          : 이 플랫폼의 관례. 전에 vLLM 만 띄울 때 PORT=8555 로 쓴 그 변수다.
    #                   그때는 serve.py 가 PORT 를 읽어 vLLM 이 8555 에 떴다. 이제는
    #                   **게이트웨이가** 그 자리에 서야 한다 — 외부에서 오는 관문이니까.
    #                   vLLM 은 안쪽으로 물러나고(VLLM_PORT, 기본 8001), 게이트웨이가
    #                   /qwen 경로로 넘겨준다. 그래서 PORT 를 vLLM 이 아니라 여기서 읽는다.
    #                   (vLLM 자식에게는 PORT=VLLM_PORT 를 따로 넣어준다)
    gw_port = int(os.environ.get("GATEWAY_PORT") or os.environ.get("PORT") or "8000")
    if os.environ.get("PORT") and not os.environ.get("GATEWAY_PORT"):
        log(f"PORT={os.environ['PORT']} 를 게이트웨이 포트로 쓴다 "
            f"(vLLM 은 VLLM_PORT={os.environ.get('VLLM_PORT', '8001')} 로 물러난다)")
    a_port = int(os.environ.get("SERVER_A_PORT", "8000"))
    b_port = int(os.environ.get("SERVER_B_PORT", "8100"))
    ui_port = int(os.environ.get("UI_PORT", "7860"))
    vllm_port = int(os.environ.get("VLLM_PORT", "8001"))
    model = os.environ.get("MAIN_LLM_MODEL", "Qwen3.6-27B")
    # 내부 서버에서 손으로 주던 파이프라인 설정. 기본값을 그때 쓴 값으로 맞춘다.
    #   POOL_STRATEGY: 코드 기본값은 "topdown" 인데 내부 서버에서는 subfallback 으로
    #   띄웠다. 여기서 안 넣으면 같은 코드가 조용히 다르게 동작한다
    #   (A/pipeline_src/pipeline/nodes.py 의 후보 풀 선택 방식이 달라진다).
    pool_strategy = os.environ.get("POOL_STRATEGY", "subfallback")

    # ── 포트 충돌 회피 (배포 모드 전용) ───────────────────────────────────────
    # IDE 모드에서는 게이트웨이(127.0.0.1)와 백엔드(POD_IP)가 인터페이스가 달라 같은
    # 포트를 써도 충돌하지 않았다. 배포 모드에서는 게이트웨이가 0.0.0.0 이고 0.0.0.0 은
    # 127.0.0.1 을 **포함**하므로, 백엔드가 같은 포트에 있으면 둘 중 하나가
    # "Address already in use" 로 죽는다 (실측 확인).
    # 게이트웨이 포트는 플랫폼이 정하므로 못 바꾼다 → 겹치는 백엔드를 옮긴다.
    # 외부에서는 게이트웨이만 보이므로 백엔드 포트가 몇 번인지는 아무 영향이 없다.
    #   ※ PORT=8001 처럼 vLLM 기본 포트를 그대로 준 경우도 여기서 걸린다.
    if mode == "deployment":
        _taken = {gw_port}

        def _avoid(label, port, fallback):
            if port != gw_port:
                _taken.add(port)
                return port
            p = fallback
            while p in _taken:
                p += 1
            _taken.add(p)
            log(f"배포 모드: {label} 를 {port} → {p} 로 옮긴다 "
                f"(게이트웨이가 0.0.0.0:{gw_port} 를 쓴다)")
            return p

        a_port = _avoid("A", a_port, 8010)
        b_port = _avoid("B", b_port, 8110)
        ui_port = _avoid("UI", ui_port, 7870)
        vllm_port = _avoid("vLLM", vllm_port, 8011)

    sec = load_or_create_secrets(Path(args.secrets_file), args.generate_secrets == "yes")
    ext = external_base_url(gw_port)

    log(f"모드={mode} "
        f"({'k8s Service 가 파드 IP 로 온다' if mode == 'deployment' else 'code-server 프록시가 루프백으로 온다'})")
    log(f"POD_IP={ip} · 모델={model} · POOL_STRATEGY={pool_strategy}")
    log(f"배치: gateway {gw_host}:{gw_port} → A {backend_host}:{a_port} · "
        f"B {backend_host}:{b_port} · UI {backend_host}:{ui_port} · vLLM {backend_host}:{vllm_port}")
    if mode == "deployment":
        log(f"배포 모드: 외부에서 들어오는 포트가 {gw_port} 여야 한다 — 배포 UI 에 지정한 "
            f"포트와 같은지 확인할 것 (다르면 GATEWAY_PORT 로 맞춘다)")
    # 웹훅 주소는 **실제로 쓰는 값을 찍는다.** 배포 UI 의 env 를 고쳐도 돌고 있는
    # 인스턴스는 옛 값을 그대로 쓰므로, 로그에 안 찍히면 "왜 저 주소로 가지?"를
    # 추적할 방법이 없다 (실제로 401 이 났을 때 옛 엔드포인트로 가고 있었다).
    # 토큰은 앞 8자만 — 맞는 토큰인지 대조는 되지만 로그에 전체가 남지는 않게.
    _wh_url = os.environ.get("PARTNER_WEBHOOK_URL", "")
    _wh_tok = os.environ.get("PARTNER_WEBHOOK_TOKEN", "")
    if _wh_url:
        log(f"파트너 웹훅 → {_wh_url}")
        log(f"  토큰(X-Webhook-Token): "
            + (f"{_wh_tok[:8]}… ({len(_wh_tok)}자)" if _wh_tok else "없음 ← 파트너가 401 을 줄 것이다"))
    else:
        log("주의: PARTNER_WEBHOOK_URL 이 비어 있다 → B 의 웹훅 전송은 꺼진 상태")

    # 모든 자식이 공유하는 기본 env. 비밀값도 여기 실어 보낸다 (인자로 주면 ps 에 남는다).
    base = os.environ.copy()
    # 비밀값 + 외부키를 모두 자식에게 넘긴다. 인자가 아니라 env 로 주는 이유는
    # 인자는 ps 출력에 남기 때문이다 (이 pod 은 웹 IDE 터미널이 열려 있다).
    #   NAVER_*/NCP_* → A(naver_api.py)와 UI(app_ui_live.py 지도)가 읽는다
    #   PARTNER_*     → B(webhook.py)가 읽는다
    base.update({k: sec.get(k, "") for k in ALL_KEYS})
    # 자식이 **실제로 받는** 값의 유무를 찍는다. 위 '배포 UI env 에서 온 값' 줄은
    # run_server 자신의 env 만 말해준다 — base.update 로 덮인 뒤 자식에게 무엇이
    # 넘어가는지는 별개다. 실제로 NCP 키가 env 에 있는데 A 는 "키가 없다"고 경고한
    # 사례가 있었고, 그때 이 줄이 없어서 어느 단계에서 비었는지 특정할 수 없었다.
    # 값은 찍지 않는다(로그에 비밀이 남지 않게). 있음/없음만 본다.
    _on = [k for k in CONFIG_KEYS if base.get(k)]
    _off = [k for k in CONFIG_KEYS if not base.get(k)]
    log(f"자식에게 전달: {', '.join(_on) if _on else '(없음)'}")
    if _off:
        log(f"자식에게 전달 안 됨(빈 값): {', '.join(_off)}")
    base["POD_IP"] = ip
    base["MAIN_LLM_MODEL"] = model
    base["POOL_STRATEGY"] = pool_strategy
    # HF 캐시를 ceph 로 못박는다. 기본값(~/.cache/huggingface)은 컨테이너 overlay 라
    # **재기동마다 사라진다** → Whisper large-v3(2.9GB)를 매번 다시 받는다.
    # (27B 는 server_piai/model 의 로컬 경로를 쓰므로 이 캐시와 무관하다)
    base.setdefault("HF_HOME", str(Path("/root/project/.cache/huggingface")))
    if ext:
        base["EXTERNAL_BASE_URL"] = ext

    want = (lambda n: args.only == n) if args.only else (lambda n: True)
    procs = Procs()
    gw_srv = None

    # SIGTERM = 배포 UI 의 '정지'. 기본 동작은 즉사라 자식들이 고아가 된다.
    def _on_signal(signum, _frame):
        log(f"신호 {signal.Signals(signum).name} 수신 — 정리한다")
        _stop.set()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        # 단계 표시: 지금 몇 번째를 하고 있는지 알려준다. 전체 개수는 옵션에 따라
        # 달라지므로(--skip-vllm 등) 먼저 셈한다.
        steps = [n for n in ("vllm", "a", "b", "ui", "gateway") if want(n)]
        if args.skip_vllm and "vllm" in steps:
            steps.remove("vllm")
        if args.skip_ui and "ui" in steps:
            steps.remove("ui")
        _step = {"i": 0}

        def step(name, title):
            _step["i"] += 1
            print("", flush=True)
            log(f"── [{_step['i']}/{len(steps)}] {title} ──────────────────────")

        # ── ⓿ GPU 사전 점검 ──────────────────────────────────────────
        # vLLM 과 A 는 GPU 가 없으면 반드시 죽는다. 죽고 나서 5번 재시작하는 대신
        # 여기서 한 번 확인하고, 무엇을 왜 건너뛰는지 명시한다.
        need_gpu = (want("vllm") and not args.skip_vllm) or want("a")
        gpu_ok = True
        # A 는 CPU 로도 돌 수 있다(느리다). 사용자가 명시적으로 CPU 를 골랐으면 존중한다.
        a_on_cpu = os.environ.get("WHISPER_DEVICE", "").strip().lower().startswith("cpu")
        if need_gpu:
            gpu_ok = gpu_available(py)
            if not gpu_ok:
                log("─" * 70)
                log("[치명] CUDA GPU 를 쓸 수 없다")
                gpu_diagnostics(py)
                log("  코드 문제가 아니라 **배포 설정** 문제다. 배포 UI 에서 이 배포에")
                log("  GPU 를 할당했는지 확인할 것 — 개발 pod 에는 있어도 배포에는")
                log("  안 붙는 경우가 있다 (JF_NUM_GPUS 값이 있어도 디바이스는 없을 수 있다).")
                log("  이 상태로 띄우면:")
                log("    · vLLM  → 'Failed to infer device type' 로 즉사")
                log("    · 서버 A → Whisper 를 GPU 로 올리다 'No CUDA GPUs are available'")
                log("  건너뛰고 나머지만 띄운다. B(민원 저장·조회)·UI·게이트웨이는 GPU 가")
                log("  없어도 정상 동작한다 → **파트너 조회 API 는 계속 응답한다.**")
                log("  음성 업로드 처리(STT·파이프라인)만 멈춘 상태가 된다.")
                if not a_on_cpu:
                    log("  A 만 CPU 로 돌리려면: WHISPER_DEVICE=cpu EMBED_DEVICE=cpu")
                    log("  (다만 27B 는 CPU 로 못 띄우므로 파이프라인은 완결되지 않는다)")
                else:
                    log("  WHISPER_DEVICE=cpu 가 지정돼 있어 A 는 CPU 로 띄운다 (느리다)")
                log("─" * 70)

        # ── ① vLLM ────────────────────────────────────────────────────
        if want("vllm") and not args.skip_vllm and gpu_ok:
            if not VLLM_SERVE.exists():
                raise SystemExit(f"[run_server] 중단: vLLM 실행 파일이 없다 → {VLLM_SERVE}")
            step("vllm", f"vLLM 기동 ({model})")
            log(f"   27B 은 가중치 로딩·컴파일·CUDA 그래프 캡처로 보통 5~20분 걸린다")
            env = dict(base, VLLM_BIND_HOST=backend_host, PORT=str(vllm_port))
            pv = procs.spawn("vllm", [py, str(VLLM_SERVE)], VLLM_SERVE.parent, env)
            # vLLM 의 /health 는 --api-key 와 무관하게 열려 있다.
            wait_ready("vllm", probe_http(f"http://{backend_host}:{vllm_port}/health"),
                       args.vllm_timeout, proc=pv, what="모델 로딩 후 /health 200")
            if _stop.is_set():
                raise KeyboardInterrupt

        # ── ② A (음성 처리) ───────────────────────────────────────────
        if want("a") and (gpu_ok or a_on_cpu):
            step("a", "서버 A 기동 (음성 처리)")
            log(f"   Whisper large-v3 · ko-sroberta · FAISS 인덱스를 올린다 (보통 1~3분)")
            log(f"   BLAS 스레드 상한 {BLAS_THREADS} (224코어 · OpenBLAS MAX_THREADS=64)")
            env = dict(
                base,
                # A → vLLM. 같은 pod 이지만 localhost 가 아니라 POD_IP 다 (vLLM 이 거기 있다).
                MAIN_LLM_URL=f"http://{backend_host}:{vllm_port}/v1",
                # A → B 내부 전송.
                SERVER_B_URL=f"http://{backend_host}:{b_port}/internal/complaints",
                SERVER_A_PORT=str(a_port),
                # UI 가 POD_IP 에서 /live_progress 를 읽는다.
                LIVE_PROGRESS_ALLOW=backend_host,
                # OpenBLAS 스레드 상한. 이게 없으면 FAISS 검색 중 세그폴트로 죽는다
                # (위 BLAS_ENV 주석 참고 — 224코어 vs MAX_THREADS=64).
                **BLAS_ENV,
            )
            pa = procs.spawn("a", [py, "-m", "uvicorn", "run_a:app",
                                    "--host", backend_host, "--port", str(a_port)],
                             A_DIR, env)
            # 예전에는 A 를 띄우고 기다리지 않았다 — 가장 오래 걸리는 놈인데 아무 표시가
            # 없어서, 업로드가 안 되면 원인이 A 인지 게이트웨이인지 알 수 없었다.
            wait_ready("a", probe_http(f"http://{backend_host}:{a_port}/health"),
                       float(os.environ.get("A_TIMEOUT", "600")), proc=pa,
                       what="모델 로딩 후 /health 200")
            if _stop.is_set():
                raise KeyboardInterrupt

        # ── ③ B (저장·조회·웹훅) ──────────────────────────────────────
        if want("b"):
            step("b", "서버 B 기동 (저장·조회·웹훅)")
            env = dict(base, SERVER_B_PORT=str(b_port))
            pb = procs.spawn("b", [py, "-m", "uvicorn", "run_b:app",
                                   "--host", backend_host, "--port", str(b_port)],
                             B_DIR, env)
            wait_ready("b", probe_http(f"http://{backend_host}:{b_port}/health"),
                       90, proc=pb, what="/health 200")

        # ── ④ 진행상황 UI ─────────────────────────────────────────────
        if want("ui") and not args.skip_ui:
            step("ui", "진행상황 UI 기동")
            if not ext:
                log("[WARN] 외부 주소를 몰라 UI 의 root_path 를 만들 수 없다 → UI 를 건너뛴다")
            else:
                env = dict(
                    base,
                    HOST=backend_host,
                    # root_path: 브라우저가 보는 전체 접두사. code-server 가
                    # /vscode/.../proxy/8000 을 떼고, 게이트웨이가 /ui 를 뗀다 → 앱에는
                    # "/" 가 도착한다. 그래서 앱이 생성 URL 에 이 전체 접두사를 다시
                    # 붙여야 자산·SSE 가 맞는다.
                    UI_ROOT_PATH=f"{ext}/ui",
                    LIVE_PROGRESS_URL=f"http://{backend_host}:{a_port}/live_progress",
                )
                pu = procs.spawn("ui", [py, "app_ui_live.py"], UI_DIR, env)
                wait_ready("ui", probe_tcp(backend_host, ui_port), 180, proc=pu,
                           what=f"{ui_port} 포트 열림")

        # ── ⑤ 게이트웨이 (이 프로세스의 스레드) ───────────────────────
        if want("gateway"):
            step("gateway", "게이트웨이 기동 (외부 입구)")
            gw_srv = start_gateway(sec.get("GATEWAY_TOKEN", ""), backend_host, gw_host, gw_port,
                                   a_port, b_port, ui_port, vllm_port)

        # ── 기동 완료 안내 ────────────────────────────────────────────
        print("", flush=True)
        print("=" * 78, flush=True)
        if ext:
            print(f"  외부 진입 주소: {ext}", flush=True)
            _auth = "(헤더 X-Gateway-Token)" if sec.get("GATEWAY_TOKEN") else "(인증 없음)"
            print(f"    업로드   POST {ext}/upload_audio     {_auth}", flush=True)
            print(f"    조회     GET  {ext}/complaints       {_auth}", flush=True)
            print(f"    최근     GET  {ext}/complaints/last  {_auth}", flush=True)
            print(f"    UI       브라우저 {ext}/ui           (Basic 로그인)", flush=True)
            print(f"    LLM      base_url {ext}/qwen/v1      {_auth}", flush=True)
        else:
            print("  외부 진입 주소를 계산할 수 없다 (JF_POD_NAME 없음)", flush=True)
        if args.print_secrets == "yes":
            print("  ── 클라이언트에 전달할 값 " + "─" * 40, flush=True)
            for _k in ("GATEWAY_TOKEN", "LLM_API_KEY", "LIVE_PROGRESS_TOKEN"):
                if sec.get(_k):
                    print(f"    {_k:19s} {sec[_k]}", flush=True)
            print(f"    UI 로그인           {sec['UI_USER']} / {sec['UI_PASS']}", flush=True)
            print(f"    (로그가 공용이면 --print-secrets no · 파일: {args.secrets_file})", flush=True)
        print(f"  로그: tail -f {LOG_DIR}/*.log", flush=True)
        # 무엇이 이 아래로 흘러나올지 미리 알려준다 — 안 보이면 정책부터 의심하게.
        _fw = {n: forward_policy(n) for n in ("a", "b", "ui", "vllm")}
        _all = [n for n, v in _fw.items() if v == "all"]
        _sel = [n for n, v in _fw.items() if isinstance(v, tuple)]
        _off = [n for n, v in _fw.items() if v is None]
        print(f"  시스템 로그 중계: 전체={','.join(_all) or '-'} · "
              f"선별={','.join(_sel) or '-'} · 끔={','.join(_off) or '-'}"
              f"   (FORWARD_LOG_PREFIXES 로 변경)", flush=True)
        print(f"    → 아래로 [a]/[b] 로 시작하는 줄이 자식 로그다. "
              f"처리시간은 [a] [TIMING] 블록.", flush=True)
        print("=" * 78, flush=True)

        # 기동 후에도 살아있다는 표시를 주기적으로 남긴다. 로그가 완전히 멈추면 "서버가
        # 죽었나" 싶어지는데, 실제로는 요청이 없어서 조용한 것뿐인 경우가 대부분이다.
        # HEARTBEAT_MIN 분마다 가동 시간과 살아있는 자식을 찍는다.
        beat_min = float(os.environ.get("HEARTBEAT_MIN", "10"))
        t_start = time.time()
        next_beat = t_start + beat_min * 60

        # 자식이 죽으면 알린다 — 조용히 하나만 죽어 있는 상태가 제일 찾기 어렵다.
        # 배포 환경에서는 프로세스를 끝내는 게 맞다 (플랫폼이 재시작하게).
        while not _stop.is_set():
            _stop.wait(3)
            for name, code in procs.dead():
                # exit=-11 은 SIGSEGV. A 가 FAISS 검색 중 OpenBLAS 스레드 고갈로
                # 이렇게 죽은 적이 있다(BLAS_ENV 주석 참고).
                sig = f" (SIGSEGV — 세그폴트)" if code == -11 else ""
                log(f"[ERROR] {name} 가 죽었다 (exit={code}){sig} — "
                    f"tail -50 {LOG_DIR / (name + '.log')}")
                tail = _last_log_line(name)
                if tail:
                    log(f"   마지막 로그: {tail}")
                if RESTART_MAX and procs.restart(name):
                    continue          # 살렸다 → 나머지는 그대로 돌아간다
                return 1
            now = time.time()
            if beat_min > 0 and now >= next_beat:
                next_beat = now + beat_min * 60
                up = int(now - t_start)
                alive = [n for n, pr, _ in procs.items if pr.poll() is None]
                log(f"♥ 가동 {up // 3600}시간 {(up % 3600) // 60}분 · 정상: "
                    f"{', '.join(alive) + ' + gateway' if alive else 'gateway'}")
    except KeyboardInterrupt:
        log("중단 요청 — 정리한다")
    finally:
        # 게이트웨이를 먼저 닫아 새 요청을 끊고, 그 다음 백엔드를 내린다.
        if gw_srv is not None:
            log("게이트웨이 닫는다")
            gw_srv.shutdown()
            gw_srv.server_close()
        procs.shutdown()
        log("종료 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
