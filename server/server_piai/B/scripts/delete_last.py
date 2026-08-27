# scripts/delete_last.py
# 실행: python scripts/delete_last.py
from pathlib import Path
import sys

# 프로젝트 루트에서 실행했는지 보장
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.database import SessionLocal
from app.db.models import Complaint

def delete_last():
    with SessionLocal() as s:
        last = s.query(Complaint).order_by(Complaint.id.desc()).first()
        if not last:
            print("[delete_last] DB가 비어 있습니다.")
            return

        print(f"[delete_last] 삭제 대상 id={last.id}, 위치={last.location}, 텍스트={last.full_text[:30]}...")
        s.delete(last)
        s.commit()
        print(f"[delete_last] id={last.id} 삭제 완료.")

if __name__ == "__main__":
    delete_last()