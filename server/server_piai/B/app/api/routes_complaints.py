# app/api/routes_complaints.py
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Query
from typing import Dict, Any
from app.db.database import SessionLocal, Base, engine, ensure_schema
from app.db.models import Complaint

router = APIRouter()

# 안전장치: 테이블 없으면 생성 + 누락 컬럼 추가 (기존 데이터는 건드리지 않음)
Base.metadata.create_all(bind=engine)
ensure_schema()

KST = timezone(timedelta(hours=9))


def _to_kst(dt: datetime | None) -> datetime | None:
    """저장된 시각을 KST(+09:00) tz-aware로 반환.
    SQLite는 timezone 정보를 보존하지 못해 naive로 읽히는데, 모든 값은 KST 벽시계로
    저장되므로(ops.now_kst) naive면 KST를 부착한다. 이미 tz-aware면 KST로 변환.
    → API 문서 계약대로 received_at이 '...+09:00' 형식으로 직렬화됨."""
    if dt is None:
        return None
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def _serialize(row: Complaint) -> Dict[str, Any]:
    """SQLAlchemy row -> dict (FastAPI가 datetime을 ISO8601로 직렬화)."""
    return {
        "id": row.id,
        "received_at": _to_kst(row.received_at),   # +09:00 오프셋 포함 (문서 계약)
        "contact": row.contact,
        "location": row.location,
        "region": row.region,                # 권역 (경상도/전라도/...)
        "odor_type": row.odor_type,
        "suspected_source": row.suspected_source,
        "intensity_change": row.intensity_change,
        "duration": row.duration,
        "full_text": row.full_text,
        "longitude": row.longitude,          # 신고자 위치 경도
        "latitude": row.latitude,            # 신고자 위치 위도
        "odor_longitude": row.odor_longitude, # 악취 위치 경도
        "odor_latitude": row.odor_latitude,   # 악취 위치 위도
    }


@router.get("/complaints")
def list_complaints(
    limit: int = Query(100, ge=1, le=1000, description="가져올 건수(최대 1000)"),
    offset: int = Query(0, ge=0, description="건너뛸 개수"),
    order: str = Query("asc", pattern="^(asc|desc)$", description="정렬: asc | desc"),
):
    """전체 민원 조회 (페이지네이션)."""
    with SessionLocal() as s:
        q = s.query(Complaint)
        total = q.count()
        if order == "asc":
            q = q.order_by(Complaint.id.asc())
        else:
            q = q.order_by(Complaint.id.desc())
        rows = q.offset(offset).limit(limit).all()

        return {
            "count": total,
            "limit": limit,
            "offset": offset,
            "order": order,
            "items": [_serialize(r) for r in rows],
        }


@router.get("/complaints/last")
def get_last_complaint():
    """가장 마지막(가장 큰 id)의 민원 1건."""
    with SessionLocal() as s:
        row = s.query(Complaint).order_by(Complaint.id.desc()).first()
        if not row:
            raise HTTPException(status_code=404, detail="No complaints found")
        return _serialize(row)