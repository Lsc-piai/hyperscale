"""
state.py
- LangGraph 파이프라인 전체에서 공유되는 State 스키마 정의
- 각 노드는 State를 입력받아 변경된 필드만 반환
  stt_text          : 입력 STT 오인식 텍스트
  audio_file        : 업로드 파일명 (few-shot 자기참조 제외용 식별자)
  estimated_region  : 추정 광역 지역 (경상도 등)
  estimated_subregion: 추정 시·군
  place_candidates  : 추출된 지명 후보 목록
  vector/ngram/jamo_results: 각 검색 방식별 결과 (태그 표시용, 최종 매칭은 통합 후보 목록 기준)
  corrected_by_place: 벡터/n-gram/자모 통합 후보로 지명 보정한 결과
  corrected_final   : few-shot RAG로 잔여 오인식 보정 결과
  normalized_text   : 사투리 → 표준어 변환 결과
  keywords          : 추출된 5개 키워드 (v2는 dict 문자열)
"""
from typing import TypedDict, List


class State(TypedDict):
    stt_text: str
    audio_file: str
    estimated_region: str
    estimated_subregion: str
    place_candidates: List[str]
    vector_results: List[dict]
    ngram_results: List[dict]
    jamo_results: List[dict]
    place_matches: List[dict]
    corrected_by_place: str
    corrected_final: str
    normalized_text: str
    keywords: str
