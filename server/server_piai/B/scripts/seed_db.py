# scripts/seed_db.py
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.database import Base, engine, SessionLocal
from app.db.models import Complaint

# 한국 시간대 (UTC+9)
KST = timezone(timedelta(hours=9))

def seed():
    Base.metadata.create_all(bind=engine)

    samples = [
        #1
        {
            "received_at": datetime(2025, 9, 4, 16, 40, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "오천힐스테이트",
            "odor_type": "해초 냄새",
            "suspected_source": "매립장",
            "intensity_change": "모름",
            "duration": "모름",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 오천힐스테이트에서 전화했어요. 냄새 때문에 전화했어요. 어떤 냄새인지 말씀해 주시겠어요? 해초 냄새 같아요, 비린 느낌이에요. 혹시 어디서 나는 것 같으신가요? 매립장 쪽에서 나는 것 같아요. 언제부터 나고 있나요? 몰라요. 이 냄새 때문에 집 안이 답답해요! 알겠습니다. 신속히 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 해결해 주세요. 감사합니다.",
        },

        #2
        {
            "received_at": datetime(2025, 9, 4, 17, 13, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "문화예술회관",
            "odor_type": "목공풀 냄새",
            "suspected_source": "모름",
            "intensity_change": "비내릴 때 확산",
            "duration": "며칠 째 불규칙",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 문화예술회관에서 전화했어요. 냄새 때문에 전화했어요. 어떤 냄새인지 구체적으로 말씀해 주시겠어요? 목공풀 냄새 같아요. 냄새가 어느 상황에서 심해지나요? 비 내릴 때 더 확산돼요. 언제부터 나고 있나요? 며칠째 불규칙하게 나요. 이 냄새 때문에 행사도 힘들어요! 알겠습니다. 현장 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 해결해 주세요. 감사합니다.",
        },

        #3
        {
            "received_at": datetime(2025, 9, 4, 17, 26, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "오천힐스테이트",
            "odor_type": "모름",
            "suspected_source": "모름",
            "intensity_change": "바람 강할 때 세짐",
            "duration": "모름",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 오천힐스테이트에서 전화했어요. 이상한 냄새 때문에 전화했어요. 어떤 냄새인지 말씀해 주시겠어요? 음, 잘 모르겠어요, 그냥 이상한 냄새예요. 바람이 강할 때 더 세져요. 알겠습니다. 말씀해 주신 정보를 바탕으로 신속히 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 해결해 주세요. 감사합니다.",
        },

        #4
        {
            "received_at": datetime(2025, 9, 5, 19, 24, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "오천힐스테이트 이마트 24 편의점",
            "odor_type": "쾌쾌한 냄새",
            "suspected_source": "모름",
            "intensity_change": "모름",
            "duration": "지난 닷새간 오전 위주",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 오천힐스테이트 이마트24편의점에서 전화했어요. 냄새 때문에 전화했어요. 어떤 냄새인지 구체적으로 말씀해 주시겠어요? 쾌쾌한 냄새 같아요, 축축한 느낌이에요. 언제부터 나고 있나요? 지난 닷새간 오전 위주로 나요. 어디서 나는 것 같으신가요? 잘 모르겠어요, 편의점 근처에서 나요. 이 냄새 때문에 손님도 줄었어요! 알겠습니다. 신속히 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 해결해 주세요. 감사합니다.",
        },

        #5
        {
            "received_at": datetime(2025, 9, 6, 12, 19, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "부영 5차",
            "odor_type": "경흥 냄새",
            "suspected_source": "경흥",
            "intensity_change": "습할 때 강렬",
            "duration": "며칠 전부터 계속",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 부영5차에서 전화했어요. 냄새 때문에 전화했어요. 경흥냄새 나요. 냄새가 어느 상황에서 심해지나요? 습할 때 강렬해요. 언제부터 나고 있나요? 며칠 전부터 계속이에요. 이 냄새 때문에 창문도 못 열어요! 알겠습니다. 신속히 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 해결해 주세요. 감사합니다.",
        },

        #6
        {
            "received_at": datetime(2025, 9, 6, 15, 13, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "오천힐스테이트",
            "odor_type": "과일 발표 냄새",
            "suspected_source": "경흥",
            "intensity_change": "오후에 약해짐",
            "duration": "멀,ㅁ",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 오천힐스테이트에서 전화했어요. 냄새 때문에 전화했어요. 어떤 냄새인지 말씀해 주시겠어요? 과일 발효 냄새 같아요, 달착지근한 느낌이에요. 혹시 어디서 나는 것 같으신가요? 경흥 쪽에서 나는 것 같아요. 냄새가 어느 시간대에 변하나요? 오후에 약해져요. 언제부터 나고 있나요? 모르겠어요. 이 냄새 때문에 집 안이 답답해요! 알겠습니다. 신속히 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 해결해 주세요. 감사합니다.",
        },

        #7
        {
            "received_at": datetime(2025, 9, 9, 8, 27, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "오천힐스테이트",
            "odor_type": "살충제 냄새",
            "suspected_source": "모름",
            "intensity_change": "모름",
            "duration": "며칠째 오후 늦게",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 오천힐스테이트에서 전화했어요. 냄새 때문에 전화했어요. 어떤 냄새인지 구체적으로 말씀해 주시겠어요? 살충제 냄새 같아요. 언제부터 나고 있나요? 며칠째 오후 늦게 나요. 어디서 나는 것 같으신가요? 잘 모르겠어요, 동네 근처에서 나요. 이 냄새 때문에 아이들 건강이 걱정돼요! 알겠습니다. 긴급히 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 와주세요. 감사합니다.",
        },

        #8
        {
            "received_at": datetime(2025, 9, 9, 10, 30, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "서희스타힐스",
            "odor_type": "발효된 치즈 냄새",
            "suspected_source": "모름",
            "intensity_change": "비 온 후 증가",
            "duration": "어제 오후부터",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 서희스타힐스에서 전화했어요. 냄새 때문에 전화했어요. 어떤 냄새인지 말씀해 주시겠어요? 발효된 치즈 냄새 같아요, 역한 느낌이에요. 냄새가 어느 상황에서 심해지나요? 비 온 후에 더 강해져요. 언제부터 나고 있나요? 어제 오후부터 나요. 이 냄새 때문에 창문도 못 열어요! 알겠습니다. 신속히 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 해결해 주세요. 감사합니다.",
        },

        #9
        {
            "received_at": datetime(2025, 9, 9, 11, 45, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "우방 2차",
            "odor_type": "매운 재 냄새",
            "suspected_source": "경흥 I&C",
            "intensity_change": "모름",
            "duration": "모름",
            "full_text": "안녕하세요, 포항시 기후대기과 악취 대응팀입니다. 어떤 도움을 드릴까요? 우방2차에서 전화했어요. 냄새 때문에 전화했어요. 어떤 냄새인지 구체적으로 말씀해 주시겠어요? 매운 재 냄새 같아요, 매캐한 느낌이에요. 혹시 어디서 나는 것 같으신가요? 경흥 쪽에서 나는 것 같아요. 언제부터 나고 있나요? 그냥 지금 미치겠어요. 이 냄새 때문에 숨쉬기가 힘들어요! 알겠습니다. 신속히 확인하고 조치하겠습니다. 추가로 알려주실 사항 있으신가요? 아니요, 빨리 해결해 주세요. 근데 공사장 소음도 심하니까 같이 해결해 주세요. 알겠습니다. 악취 문제 먼저 확인하겠습니다. 감사합니다.",
        },

        #10
        {
            "received_at": datetime(2025, 9, 10, 9, 15, tzinfo=KST),
            "contact": "010-0000-0000",
            "location": "우방 2차",
            "odor_type": "똥냄새",
            "suspected_source": "모름",
            "intensity_change": "아침 강렬",
            "duration": "모름",
            "full_text": "안녕하세요 포항시 기후대기과 악취 대응팀입니다 어떤 도움을 드릴까요 우방2차에서 전화했어요 똥냄새 나요 정말 심해요 아침에 특히 강렬해요 이 냄새 때문에 창문도 못 열어요 알겠습니다 신속히 확인하고 조치하겠습니다 추가로 알려주실 사항 있으신가요 아니요 빨리 해결해 주세요 감사합니다",
        },
        # 필요하면 계속 추가
    ]

    with SessionLocal() as s:
        current_count = s.query(Complaint).count()
        if current_count > 0:
            print(f"[seed] 이미 {current_count}건 존재합니다. 시드를 건너뜁니다.")
            return

        objs = [Complaint(**row) for row in samples]
        s.add_all(objs)
        s.commit()
        print(f"[seed] {len(samples)}건 삽입 완료.")

if __name__ == "__main__":
    seed()