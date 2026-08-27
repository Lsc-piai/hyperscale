# app/core/live_progress.py  (서버 A)
"""
파이프라인 진행 상황을 파일로 공유하는 모듈.
- 서버 A(process_fn)가 한 통화를 처리하면서 단계별 중간 결과를 JSON 파일에 기록한다.
- 별도로 뜬 Gradio UI(A/ui/app_ui_live.py)가 이 파일을 1초마다 읽어 단계별로 표시한다.
  (client_uploader → 서버 A 처리 과정을 7860 UI에서 실시간으로 보기 위함)
- 원자적 쓰기(os.replace)로 UI가 반쯤 쓰인 파일을 읽지 않게 한다.
- LIVE_PROGRESS_PATH 환경변수로 파일 경로를 바꿀 수 있다(기본 A/data/live_progress.json).
"""
import json
import os
from pathlib import Path

# UI 진행 단계 라벨 (UI와 공유). 인덱스 0..7
STEPS = ["STT 변환", "시/군 추정", "광역 추정", "지명 추출",
         "지명 보정", "잔여 보정", "표준어 변환", "키워드 추출"]
KW_LABELS = ["신고자 위치", "냄새 종류", "냄새 강도", "냄새 주기", "냄새 위치"]

# 그래프 노드 완료 시 기록할 step (steps[step-1]까지 done, steps[step] 진행 중; 8 = 전부 완료)
NODE_STEP = {
    "remove_noise": 1,
    "estimate_subregion": 2, "estimate_region": 3, "extract_places": 4,
    "rag_search": 5, "correct_remaining": 6, "normalize_dialect": 7,
    "extract_keywords": 8,
}

PROGRESS_PATH = Path(os.environ.get(
    "LIVE_PROGRESS_PATH",
    str(Path(__file__).resolve().parents[2] / "data" / "live_progress.json"),
))


def write_progress(data: dict) -> None:
    """진행 상황 dict를 원자적으로 파일에 기록."""
    try:
        PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROGRESS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PROGRESS_PATH)
    except Exception as e:
        print(f"[LIVE_PROGRESS][WARN] 기록 실패: {e}")


def read_progress():
    """진행 상황 dict 반환. 없거나 읽기 실패 시 None."""
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fmt_search(entries: list) -> str:
    parts = []
    for entry in entries or []:
        parts.append(f"[후보: {entry.get('candidate', '')}]  (★ = 추정 시군 내 장소)")
        for i, r in enumerate((entry.get("results") or [])[:10], 1):
            mark = "★" if r.get("in_subregion") else "  "
            parts.append(f" {i:2d}. {mark}{r.get('place_name', '')}  |  {r.get('road_address', '')}")
        parts.append("")
    return "\n".join(parts) if parts else "(검색 결과 없음)"


def _fmt_matches(matches: list) -> str:
    if not matches:
        return "(지명 후보 없음)"
    lines = []
    for m in matches:
        addr = f"  ({m['road_address']})" if m.get("road_address") else ""
        lines.append(f"{m.get('candidate', '')}  →  {m.get('picked', '')}{addr}")
    return "\n".join(lines)


def build_snapshot(step: int, state: dict, error=None, file: str = "", job: str = "",
                   extra: dict = None) -> dict:
    """파이프라인 state에서 UI 표시용 진행 상황 dict를 만든다.
    job: 통화(작업)마다 고유한 토큰. UI가 이 값의 변화로 '새 업로드'를 감지해 알림을 띄운다.
    extra: outputs 에 덧붙일 값. 지오코딩 좌표처럼 그래프 state 밖에서 나오는 것을 넘긴다."""
    kw_str = state.get("keywords", "") or ""
    kw_pred = [""] * 5
    if kw_str:
        try:
            from evaluation.keyword_eval import parse_kw_fields  # A 처리 시에만 로드 (UI는 미사용)
            kw_pred = parse_kw_fields(kw_str)
        except Exception:
            pass
    outputs = {
        "file": file,
        "stt": state.get("stt_text", "") or "",
        # STT 진행 중 누적 전사문(step 0에서만 채워짐). UI 가 단어 단위로 타이핑해 보여준다.
        # 파이프라인이 시작되면(step≥1) 안 보내므로 UI 는 원래 말풍선 표시로 돌아간다.
        "stt_stream": "",
        # STT 전사 완료 후 화자분리(분석) 중 표식. True 면 UI 가 "민원 분석중…" 스피너를 띄운다.
        "stt_analyzing": False,
        "subregion": state.get("estimated_subregion", "") or "",
        "region": state.get("estimated_region", "") or "",
        "places": "\n".join(state.get("place_candidates", []) or []),
        "search_vec": _fmt_search(state.get("vector_results", [])),
        "search_ngram": _fmt_search(state.get("ngram_results", [])),
        "search_jamo": _fmt_search(state.get("jamo_results", [])),
        "matches": _fmt_matches(state.get("place_matches", [])),
        "rag": state.get("corrected_by_place", "") or "",
        "corrected": state.get("corrected_final", "") or "",
        # 보정 칸의 '표준어 체크' 전용 판본 (같은 보정문인데 표식만 다르다).
        # 두 벌로 나눠 그리는 이유는 lifespan._disp 주석 참고 — 박스 연결 때문.
        "corrected_nor": state.get("corrected_final_nor", "") or "",
        "normalized": state.get("normalized_text", "") or "",
        "keywords": kw_str,
        "kw_pred": list(kw_pred),
        # 지도 마커 좌표(신고자·냄새). 지오코딩 결과라 파이프라인이 extra 로 넣어준다
        # (키워드에서 위치명이 나온 뒤 마지막 스냅샷에 채워진다). 못 찾으면 None.
        # 빈 지도(서비스 지역) 자체는 좌표 없이도 UI 가 file 유무만 보고 띄운다.
        "rep_lon": None, "rep_lat": None, "rep_name": "",
        "odor_lon": None, "odor_lat": None, "odor_name": "",
    }
    if extra:
        outputs.update(extra)
    return {"step": step, "error": error, "job": job, "outputs": outputs}
