# app/core/server_graph.py  (서버 A)
"""
서버용 LangGraph 파이프라인
- raw/whisper/main 의 graph_qwen.py 에서 서버 운영에 필요한 구간만 발췌한 그래프.
  (원본: load_stt → ... → judge_keywords → geo_lookup)
- 차이점:
  * load_stt 제거      — dataset CSV 랜덤 샘플 대신, 업로드된 오디오의 STT 텍스트를 initial state로 직접 주입
  * judge_keywords 제거 — 정답(GT) 키워드가 없는 실서비스 입력이므로 판정 불가
  * geo_lookup 제거    — 위경도 조회는 서버 B가 저장 시점에 수행 (기존 역할 분담 유지)
- 흐름:
    remove_noise → estimate_subregion → estimate_region → extract_places
    → rag_search → correct_remaining → normalize_dialect → extract_keywords → END
  (remove_noise: STT 직후 방송환각·반복중복 제거 — 이후 모든 단계가 깨끗한 텍스트를 보게 함)
"""
import time

from langgraph.graph import StateGraph, END

# 자립형 파이프라인 (A/pipeline_src, config.py에서 sys.path 주입 후 import 가능)
from app.core.config import PIPELINE_DIR  # noqa: F401  (sys.path 주입 보장용)
from pipeline.state import State
from pipeline.nodes import (
    remove_noise, estimate_region, estimate_subregion, extract_places,
    rag_search, correct_remaining, normalize_dialect,
    extract_keywords, extract_keywords_v2,
)

KEYWORD_NODES = {1: extract_keywords, 2: extract_keywords_v2}


def _timed_node(name, fn):
    """각 STEP 의 벽시계 시간을 찍는다(요청당 병목 분해용). fn 반환/예외는 그대로 통과."""
    def wrapped(state):
        t0 = time.perf_counter()
        try:
            return fn(state)
        finally:
            print(f"[TIME] STEP {name}: {(time.perf_counter() - t0) * 1000:.0f} ms", flush=True)
    return wrapped


def build_server_graph(keyword_version: int = 1):
    keyword_fn = KEYWORD_NODES[keyword_version]

    graph = StateGraph(State)

    for name, fn in [
        ("remove_noise", remove_noise),
        ("estimate_subregion", estimate_subregion),
        ("estimate_region", estimate_region),
        ("extract_places", extract_places),
        ("rag_search", rag_search),
        ("correct_remaining", correct_remaining),
        ("normalize_dialect", normalize_dialect),
        ("extract_keywords", keyword_fn),
    ]:
        graph.add_node(name, _timed_node(name, fn))

    graph.set_entry_point("remove_noise")
    graph.add_edge("remove_noise", "estimate_subregion")
    graph.add_edge("estimate_subregion", "estimate_region")
    graph.add_edge("estimate_region", "extract_places")
    graph.add_edge("extract_places", "rag_search")
    graph.add_edge("rag_search", "correct_remaining")
    graph.add_edge("correct_remaining", "normalize_dialect")
    graph.add_edge("normalize_dialect", "extract_keywords")
    graph.add_edge("extract_keywords", END)

    return graph.compile()


def make_initial_state(stt_text: str, audio_file: str = "") -> State:
    """업로드 오디오의 STT 결과로 파이프라인 initial state 구성."""
    return {
        "stt_text": stt_text,
        "audio_file": audio_file,      # few-shot 자기참조 제외용 식별자 (업로드 파일명)
        "estimated_region": "",
        "estimated_subregion": "",
        "place_candidates": [],
        "vector_results": [],
        "ngram_results": [],
        "jamo_results": [],
        "place_matches": [],
        "corrected_by_place": "",
        "corrected_final": "",
        "normalized_text": "",
        "keywords": "",
    }
