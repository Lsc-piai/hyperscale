# scripts/list_db.py
# 실행: python scripts/list_db.py

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.database import SessionLocal, Base, engine
from app.db.models import Complaint


def list_all():
    # 테이블이 없으면 생성 (안전장치)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as s:
        rows = s.query(Complaint).order_by(Complaint.id.asc()).all()
        if not rows:
            print("[list_db] 현재 DB에 저장된 민원이 없습니다.")
            return

        print(f"[list_db] 총 {len(rows)} 건")
        print("-" * 80)
        for row in rows:
            print(
                f"ID={row.id} | 접수시간={row.received_at} | 연락처={row.contact}\n"
                f"  발생지={row.location}\n"
                f"  냄새종류={row.odor_type}\n"
                f"  원인추정={row.suspected_source}\n"
                f"  강도변화={row.intensity_change}\n"
                f"  지속시간={row.duration}\n"
                f"  전체텍스트={row.full_text}\n"
                + "-" * 80
            )


if __name__ == "__main__":
    list_all()