# app/db/ops.py
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
from .database import SessionLocal
from .models import Complaint

# (선택) 쓰기 직렬화를 원하면 사용
_WRITE_LOCK = threading.RLock()


def now_kst() -> datetime:
    """KST(+09:00) 타임존 정보가 포함된 datetime 반환."""
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).replace(microsecond=0)


@contextmanager
def db_session():
    """요청마다 새 세션 열고 닫기 (스레드 세이프)."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except:
        s.rollback()
        raise
    finally:
        s.close()


def save_complaint(
    *,
    full_text: str,
    location: str | None = None,
    region: str | None = None,
    odor_type: str | None = None,
    suspected_source: str | None = None,
    intensity_change: str | None = None,
    duration: str | None = None,
    contact: str = "010-0000-0000",
    longitude: float | None = None,
    latitude: float | None = None,
    odor_longitude: float | None = None,
    odor_latitude: float | None = None,
    received_at: datetime | None = None,
    return_full: bool = False,
) -> int | dict:
    """
    민원 1건 저장
    - 기존 데이터: longitude / latitude = NULL 허용
    - 신규 데이터: 좌표 포함 가능
    """

    if received_at is None:
        received_at = now_kst()

    with _WRITE_LOCK:
        with db_session() as s:
            # 현재 DB 최대 ID 조회 (기존 정책 유지)
            max_id = s.query(Complaint.id).order_by(Complaint.id.desc()).first()
            new_id = (max_id[0] if max_id else 0) + 100

            obj = Complaint(
                id=new_id,                       # 👈 기존 ID 정책 유지
                received_at=received_at,
                contact=contact,
                location=location,
                region=region,
                odor_type=odor_type,
                suspected_source=suspected_source,
                intensity_change=intensity_change,
                duration=duration,
                full_text=full_text,
                longitude=longitude,
                latitude=latitude,
                odor_longitude=odor_longitude,
                odor_latitude=odor_latitude,
            )

            s.add(obj)
            s.flush()

            if return_full:
                return {
                    "id": obj.id,
                    "received_at": obj.received_at,
                }

            return obj.id