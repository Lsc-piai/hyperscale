"""
keyword_eval.py  (서버 A용 축소판)
- KW_KEYS         : 키워드 5개 필드명 (extract_keywords_v2 출력 순서)
- parse_kw_fields : extract_keywords_v2 dict 문자열 → 5개 필드 값 리스트
※ raw 원본의 판정(judge)·위경도 조회(geo) 함수는 서버 A가 쓰지 않아 제거했다.
  지명 정제·지역 부착은 키워드 추출 LLM(keywords_v2_prompt)이 함께 처리한다.
"""
import ast

KW_KEYS = ["신고자 위치", "냄새 종류", "냄새 강도", "냄새 주기", "냄새 위치"]


def parse_kw_fields(keywords_str: str) -> list:
    """extract_keywords_v2의 dict 문자열 출력을 KW_KEYS 순서의 5개 필드 값 리스트로 파싱.
    파싱 불가(v1 모델 출력 등)면 빈 값 5개 반환."""
    try:
        d = ast.literal_eval(keywords_str)
        if isinstance(d, dict):
            values = []
            for k in KW_KEYS:
                v = d.get(k, "")
                values.append(", ".join(map(str, v)) if isinstance(v, list) else str(v))
            return values
    except (ValueError, SyntaxError):
        pass
    return [""] * 5
