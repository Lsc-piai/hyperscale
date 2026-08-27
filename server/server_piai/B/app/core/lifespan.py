# app/core/lifespan.py  (서버 B)
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db.database import engine, Base

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 DB 테이블 보장
    Base.metadata.create_all(bind=engine)
    yield