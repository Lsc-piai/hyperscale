# app/db/models.py
from sqlalchemy import Column, Integer, String, Text, DateTime, Float
from .database import Base

class Complaint(Base):
    __tablename__ = "complaints"

    id               = Column(Integer, primary_key=True, index=True)

    # 접수 시각 (KST, timezone-aware)
    received_at      = Column(DateTime(timezone=True), nullable=False)
    contact          = Column(String, default="010-0000-0000")
    location         = Column(String, nullable=True)          # 민원발생지
    region           = Column(String, nullable=True)          # 권역 (경상도/전라도/충청도/강원도/제주도)
    odor_type        = Column(String, nullable=True)          # 냄새 종류
    suspected_source = Column(String, nullable=True)          # 원인 추정 지역
    intensity_change = Column(String, nullable=True)          # 냄새 강도의 변화
    duration         = Column(String, nullable=True)          # 냄새 지속시간

    full_text        = Column(Text, nullable=False)           # 전체 민원 텍스트

    # 신고자 위치(민원인이 있는 곳) 좌표 (기존 데이터는 NULL)
    latitude         = Column(Float, nullable=True)           # 신고자 위치 위도
    longitude        = Column(Float, nullable=True)           # 신고자 위치 경도

    # 악취(냄새) 발생 추정 위치 좌표 (기존 데이터는 NULL)
    odor_latitude    = Column(Float, nullable=True)           # 악취 위치 위도
    odor_longitude   = Column(Float, nullable=True)           # 악취 위치 경도