# app/api/routes_receive.py  (서버 B)
"""
서버 A → 서버 B 내부 연동 API
- payload 기준: full_text(보정+표준어 변환 텍스트), stt_text(STT 원문, 저장 안 함),
  location(신고자 위치)/odor_type/suspected_source(냄새 위치)/intensity_change/duration, contact,
  longitude·latitude(신고자 위치 좌표), odor_longitude·odor_latitude(냄새 위치 좌표)
- 위치 처리(도로명 조회·위경도)는 A가 담당. B는 받은 값을 그대로 저장만 한다.
"""
from fastapi import APIRouter, BackgroundTasks
from app.db.ops import save_complaint
from app.core.webhook import send_partner_webhook_sync

router = APIRouter()


def _fire_webhook(created_ids, created_at_iso):
    """파트너 웹훅 발송 (백그라운드 실행). 실패해도 접수엔 영향 없음."""
    try:
        send_partner_webhook_sync(created_ids, created_at_iso=created_at_iso)
    except Exception as e:
        print("[WEBHOOK ERROR]", e)


@router.post("/internal/complaints")
def receive_from_server_a(payload: dict, background_tasks: BackgroundTasks):
    # 위경도(신고자·악취 위치)는 A가 조회해 payload로 넘겨줌 (없으면 0). B는 저장만.
    rec = save_complaint(
        full_text=payload["full_text"],
        location=payload.get("location"),
        region=payload.get("region"),          # 권역 (A의 파이프라인이 추정)
        odor_type=payload.get("odor_type"),
        suspected_source=payload.get("suspected_source"),
        intensity_change=payload.get("intensity_change"),
        duration=payload.get("duration"),
        contact=payload.get("contact", "010-0000-0000"),
        longitude=payload.get("longitude"),          # 없으면 None(빈칸)
        latitude=payload.get("latitude"),
        odor_longitude=payload.get("odor_longitude"),
        odor_latitude=payload.get("odor_latitude"),
        return_full=True,
    )

    # 3) 파트너 웹훅은 백그라운드로 (재시도로 오래 걸려도 A 응답을 막지 않도록).
    #    저장은 이미 끝났으니 웹훅이 실패해도 데이터는 안전.
    background_tasks.add_task(
        _fire_webhook,
        [rec["id"]],
        rec["received_at"].isoformat(timespec="seconds"),
    )

    # 저장 직후 즉시 응답 → A의 타임아웃 방지
    return {"ok": True, "id": rec["id"]}
