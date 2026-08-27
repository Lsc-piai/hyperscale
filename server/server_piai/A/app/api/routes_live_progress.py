# app/api/routes_live_progress.py  (서버 A)
"""
진행상황 JSON 을 HTTP 로 읽게 해 주는 읽기 전용 엔드포인트.

UI(`A/ui/app_ui_live.py`)는 원래 서버 A 와 같은 머신에서 `live_progress.json` 파일을
직접 읽는다. UI 를 다른 머신에서 돌릴 때 그 내용을 받을 통로가 이것이다.

■ 접근 제어
  진행상황에는 민원 전사문·주소가 들어 있으므로 기본은 **루프백만** 허용한다.
  다른 출처에서 붙여야 하면 `LIVE_PROGRESS_ALLOW` 에 IP/CIDR 을 쉼표로 주고,
  `LIVE_PROGRESS_TOKEN` 을 함께 설정하면 `X-Live-Token` 헤더 검사가 한 겹 더 얹힌다
  (둘 다 통과해야 응답한다).

■ 거절은 404 다
  없는 경로처럼 보이게 한다.
"""
import ipaddress
import os
import secrets

from fastapi import APIRouter, HTTPException, Request

from ..core.live_progress import read_progress

router = APIRouter()

_TOKEN = os.environ.get("LIVE_PROGRESS_TOKEN", "")


def _allow_nets():
    nets = []
    for part in os.environ.get("LIVE_PROGRESS_ALLOW", "").split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                print(f"[live_progress][WARN] LIVE_PROGRESS_ALLOW 항목을 못 읽었다: {part!r}")
    return nets


_ALLOW = _allow_nets()


def _permitted(peer: str) -> bool:
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_loopback or any(ip in net for net in _ALLOW)


@router.get("/live_progress")
def live_progress(request: Request):
    peer = request.client.host if request.client else ""
    if not _permitted(peer):
        print(f"[live_progress] 거절 {peer} (루프백 아님, LIVE_PROGRESS_ALLOW 밖)", flush=True)
        raise HTTPException(status_code=404)
    # 토큰은 IP 검사에 더해지는 것이다(대체가 아니다). 미설정이면 이 검사만 건너뛴다.
    # compare_digest 로 비교해 타이밍으로 앞자리를 맞춰가는 공격을 막는다.
    if _TOKEN and not secrets.compare_digest(
            request.headers.get("X-Live-Token", ""), _TOKEN):
        print(f"[live_progress] 거절 {peer} (X-Live-Token 불일치)", flush=True)
        raise HTTPException(status_code=404)
    # 파일이 아직 없으면 None → UI 는 '대기 중' 화면을 그린다. 200 으로 준다.
    return read_progress()
