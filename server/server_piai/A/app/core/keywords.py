# app/core/keywords.py  (서버 A)
"""
파이프라인 키워드 출력 → 서버 B payload 필드 변환
- v1 (파인튜닝 Qwen3-0.6B): "1. 민원발생지: ..., 2. 냄새종류: ..., ..." 형식 (기존 서버와 동일 포맷)
- v2 (Qwen3.6-27B 프롬프트): KW_KEYS(신고자 위치/냄새 종류/냄새 강도/냄새 주기/냄새 위치) dict 문자열
- 서버 B의 DB 컬럼(location, odor_type, suspected_source, intensity_change, duration)은
  기존 스키마를 유지하므로 두 형식 모두 여기서 같은 필드명으로 매핑한다.
"""
import re

from app.core.config import PIPELINE_DIR  # noqa: F401  (sys.path 주입 보장용)
from evaluation.keyword_eval import parse_kw_fields

_EMPTY = "모름"

# v1 출력 파싱 (기존 서버 A lifespan.py의 정규식 그대로)
_V1_PATTERN = (
    r"1\. 민원발생지:\s*(.*?),\s*2\. 냄새종류:\s*(.*?),\s*"
    r"3\. 원인추정지역:\s*(.*?),\s*4\. 냄새강도의 변화:\s*(.*?),\s*5\. 냄새 지속시간:\s*(.*)"
)


def _clean(v: str) -> str:
    v = v.strip().rstrip(") ")
    return v if v else _EMPTY


def parse_keywords_v1(keywords_str: str) -> dict:
    m = re.search(_V1_PATTERN, keywords_str)
    if not m:
        print("[keywords] v1 출력에서 키워드를 추출하지 못했습니다.")
        return {
            "location": _EMPTY, "odor_type": _EMPTY, "suspected_source": _EMPTY,
            "intensity_change": _EMPTY, "duration": _EMPTY,
        }
    return {
        "location": _clean(m.group(1)),          # 민원발생지
        "odor_type": _clean(m.group(2)),         # 냄새종류
        "suspected_source": _clean(m.group(3)),  # 원인추정지역
        "intensity_change": _clean(m.group(4)),  # 냄새강도의 변화
        "duration": _clean(m.group(5)),          # 냄새 지속시간
    }


def parse_keywords_v2(keywords_str: str) -> dict:
    # KW_KEYS 순서: [신고자 위치, 냄새 종류, 냄새 강도, 냄새 주기, 냄새 위치]
    fields = parse_kw_fields(keywords_str)
    return {
        "location": _clean(fields[0]),          # 신고자 위치
        "odor_type": _clean(fields[1]),         # 냄새 종류
        "intensity_change": _clean(fields[2]),  # 냄새 강도
        "duration": _clean(fields[3]),          # 냄새 주기
        "suspected_source": _clean(fields[4]),  # 냄새 위치 (원인 추정 장소)
    }


def _norm(v: str) -> str:
    """중복 비교용 정규화 (공백 제거)."""
    return re.sub(r"\s+", "", v or "")


def parse_keywords(keywords_str: str, version: int) -> dict:
    d = parse_keywords_v2(keywords_str) if version == 2 else parse_keywords_v1(keywords_str)
    # 강도·주기 중복 방어: LLM이 "밤 10시 넘으면 심해진다"류(시간대별 세기 변화)를
    # 강도·주기 두 곳에 똑같이 넣는 경우가 있음. 두 값이 사실상 같으면 세기 변화이므로
    # 강도만 남기고 주기는 미언급 처리.
    if d["duration"] not in ("미언급", _EMPTY) and _norm(d["duration"]) == _norm(d["intensity_change"]):
        d["duration"] = "미언급"
    # 위치 중복 방어: 발생원을 특정하지 못했을 때 신고자 위치를 냄새 위치에 그대로
    # 복사하는 경우가 있음(노이즈 800건 전수검사에서 냄새 위치를 채운 180건 중 83건이
    # 신고자 위치와 동일했고, 그 83건은 정답이 전부 "미언급"이었다).
    # 두 값이 사실상 같으면 발생원을 지목한 게 아니므로 냄새 위치를 미언급 처리한다.
    if d["suspected_source"] not in ("미언급", _EMPTY) and \
            _norm(d["suspected_source"]) == _norm(d["location"]):
        d["suspected_source"] = "미언급"
    return d
