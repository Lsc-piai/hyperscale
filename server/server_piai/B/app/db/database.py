# app/db/database.py
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from pathlib import Path

# ./data/app_data.db 로 고정 (디렉토리 생성)
DATA_DIR = Path("./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite:///{(DATA_DIR / 'app_data.db').as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # 멀티스레드 허용
    pool_pre_ping=True,                         # 죽은 커넥션 자동 감지
    future=True,
)

# 커넥션 생성 시, WAL/PRAGMA 적용
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, conn_record):
    cur = dbapi_conn.cursor()
    # 동시성 ↑
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    # 무결성
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)

Base = declarative_base()


def ensure_schema():
    """기존 DB(app_data.db)에 없는 컬럼을 추가하는 가벼운 마이그레이션.
    - SQLite는 create_all로 기존 테이블에 컬럼을 추가하지 못하므로 직접 ALTER 한다.
    - 기존 행 데이터는 건드리지 않는다 (새 컬럼은 NULL)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if not insp.has_table("complaints"):
        return  # 테이블이 아직 없으면 create_all이 최신 스키마로 만들어줌
    existing = {c["name"] for c in insp.get_columns("complaints")}
    to_add = {"odor_latitude": "FLOAT", "odor_longitude": "FLOAT", "region": "VARCHAR"}
    with engine.begin() as conn:
        for col, typ in to_add.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE complaints ADD COLUMN {col} {typ}"))
                print(f"[DB migration] complaints 테이블에 {col} 컬럼 추가")