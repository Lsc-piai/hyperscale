"""
nodes.py
- LangGraph 보정 파이프라인 노드 함수 정의 (프롬프트는 prompts.py, 평가는 keyword_eval.py)
- STEP 1.5 remove_noise     : STT 직후, 지역/지명 추정 '전에' 방송 환각·반복 중복 제거
                              (이후 모든 LLM 판단이 깨끗한 텍스트를 보게 하기 위함)
- STEP 2a estimate_subregion: LLM으로 시·군 추정
- STEP 2b estimate_region   : 추정된 시·군 + STT 텍스트 + 지역별 대표 종결어미를 함께 LLM에게 주고 광역 방언권 판단
- STEP 2c extract_places    : LLM으로 장소명 후보 추출 (민원인 위치 / 냄새 발생 추정 위치)
- STEP 3  rag_search        : 지역 FAISS DB에서 벡터/n-gram/자모 검색 결과를 태그로 통합해 LLM이 한 번에 지명 보정
- STEP 4  correct_remaining : stt_err FAISS few-shot으로 잔여 오인식 보정
- STEP 5  normalize_dialect : LLM으로 사투리 → 표준어 변환
- STEP 6  extract_keywords  : 파인튜닝 Qwen(0.6B, 8002) 또는 27B(8001) 프롬프트로 5개 키워드 추출
"""
import difflib
import os
import re
import threading
import time

from pipeline import prompts
from core.config import REGIONS, REGION_ENDINGS
from evaluation.keyword_eval import KW_KEYS
from core.llm import llm, kw_llm
from core.search import (load_faiss, subregion_count, search_vector,
                         search_ngram, search_ngram_sparse, search_bm25,
                         search_jamo, search_jamo_sparse, search_stt_err)
from core.text_utils import chosung_score
from pipeline.state import State


def sep(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# 지역 DB의 '공백·기호 제거' 표기 → 항목 (지역별 1회 구축 후 재사용).
# STEP3의 원본-유지 판정에서 700k건을 매번 훑지 않기 위한 캐시.
# 락 필수: 배치 처리처럼 여러 스레드가 같은 지역에 동시에 도달하면 각 스레드가
# 71만건(경상도) 인덱스를 중복 구축하며 GIL을 붙잡아 전체가 멈춘다.
_db_name_index: dict = {}
_db_index_lock = threading.Lock()

_NORM_RE = re.compile(r"[\s.\-]")


def _norm_place_name(s: str) -> str:
    return _NORM_RE.sub("", (s or "").strip())


def _db_exact(region: str, metadata: list, candidate: str):
    """후보 표기가 DB에 그대로(공백·기호 무시) 존재하면 그 항목을 돌려준다."""
    idx = _db_name_index.get(region)
    if not idx:
        with _db_index_lock:
            idx = _db_name_index.get(region)      # 락 획득 후 재확인
            if not idx:
                idx = {}
                for item in metadata:
                    key = _norm_place_name(item.get("place_name", ""))
                    if key and key not in idx:
                        idx[key] = item
                _db_name_index[region] = idx
    return idx.get(_norm_place_name(candidate))


def _protected_names(state: State) -> list:
    """STEP3에서 공식 명칭으로 확정된 지명 목록 (후속 LLM 단계에서 변형 금지 대상)."""
    return [
        m["picked"] for m in state.get("place_matches", [])
        if m.get("picked") and m["picked"] != "없음"
    ]


def _to_dialogue(text: str) -> str:
    """LLM으로 화자(상담원/민원인)를 구분해 발화별로 줄을 나누고 라벨을 붙인다.
    이후 표준어 변환·키워드·화면 표시가 모두 이 '대화 형태'를 공유한다 → 변환 시
    발화가 줄로 나뉘어 어미 누락이 줄고, 파이프라인·화면 결과가 일관된다.
    발화 사이에 빈 줄 하나(간격용). 실패 시 원문 그대로."""
    if not text:
        return text
    try:
        out = llm([{"role": "user", "content": prompts.format_dialogue_prompt(text)}]) or text
    except Exception as e:
        print(f"[FORMAT][WARN] 화자분리 실패: {e}")
        out = text
    return "\n\n".join(ln for ln in out.split("\n") if ln.strip())


def remove_noise(state: State) -> State:
    """STEP 1.5 | LLM - STT 잡음(방송·영상 환각 + 반복 중복) 제거.
    STT 직후, 지역·지명 추정 '전에' 청소한다 — 이후 STEP2a~4가 모두 이 깨끗한 stt_text를
    쓰게 되어(state 갱신) 오염된 텍스트로 인한 오분류/오추출 위험을 줄인다.
    (예전엔 STEP4 앞에서만 청소했으나, 그보다 앞선 추정·매칭 단계도 오염 텍스트를 보고 있어 이동함.
     remove_hallucination_prompt는 region을 쓰지 않으므로 지역 추정 전에 돌려도 무방)"""
    sep("STEP 1.5 | LLM - STT 잡음(환각+반복) 제거")
    text = state["stt_text"]
    cleaned = llm([{"role": "user",
                    "content": prompts.remove_hallucination_prompt(text)}],
                  max_tokens=1024).strip()
    if len(cleaned) < 0.5 * len(text):   # 과삭제/실패 시 원문 유지(안전장치)
        print(f"[STEP1.5][WARN] 환각/중복 제거 출력 비정상({len(cleaned)}/{len(text)}자) → 원문 유지")
        cleaned = text
    print(f"[STEP1.5] 환각/중복 제거: {len(text)} → {len(cleaned)}자")
    return {**state, "stt_text": cleaned}


def estimate_subregion(state: State) -> State:
    sep("STEP 2a | LLM - 시/군 추정")
    raw = llm([{"role": "user", "content": prompts.subregion_prompt(state["stt_text"])}], temperature=0)
    print(f"[LLM 응답] {raw}")

    INVALID = {"없음", "모름", "불명", "추정", "불가", "미상", "알수없음"}
    subregion = ""
    raw_clean = raw.strip().split()[0] if raw.strip() else ""
    if raw_clean and raw_clean not in INVALID and len(raw_clean) <= 6:
        subregion = raw_clean

    print(f"→ 추정 시군: {subregion if subregion else '(없음)'}")
    return {**state, "estimated_subregion": subregion}


def estimate_region(state: State) -> State:
    sep("STEP 2b | LLM - 광역 지역 추정 (시/군 + 어미 패턴 기반)")
    ending_lines = "\n".join(f"- {r}: {', '.join(e)}" for r, e in REGION_ENDINGS.items())
    prompt = prompts.region_prompt(state["stt_text"], state["estimated_subregion"], ending_lines)

    raw = llm([{"role": "user", "content": prompt}], temperature=0)
    print(f"[LLM 응답] {raw}")

    region = "경상도"
    for r in REGIONS:
        if r in raw:
            region = r
            break

    print(f"→ 추정 광역: {region}")
    return {**state, "estimated_region": region}


# 지명이 아니라 **LLM 이 자기 상황을 설명한 줄**을 후보에서 걸러내는 패턴.
# places_prompt 는 "없으면 그 줄은 아예 출력하지 마세요"라고 지시하지만, 모델은 종종 그
# 지시문을 그대로 되돌려주거나 "(해당 위치가 텍스트에 없음)", "따라서 규칙에 따라 출력을
# 하지 않습니다." 같은 문장을 낸다. 노이즈 800건 결과에서 후보 1030개 중 17종·38건이 이런
# 줄이었다(3.7%).
#
# 왜 걸러야 하나 (단순히 낭비만이 아니다):
#   · 이 줄 하나마다 71만 지명 대상 검색(벡터+n-gram+자모)과 pick_best LLM 호출 2회가 돈다.
#   · 실제로 오매칭이 났다: '해당 위치가 텍스트에 없으면 그 줄은 아예 출력하지 마세요'
#     → '힐스테이트 강릉아파트'(강원도_id66, 정답은 강릉회산LH천년나무2단지). 잘못 고른 이름은
#     _protected_names 로 들어가 이후 보정·표준어·키워드 프롬프트에 '확정된 공식 명칭'으로
#     주입된다. 텍스트 치환(replace)은 후보가 원문에 없어 조용히 넘어가므로 눈에 안 띈다.
#
# ※ '원문에 등장하지 않는 후보를 버린다'는 더 강한 규칙은 쓰지 않는다. 800건에서 원문에 없는
#    후보 55건 중 24건이 **정상 후보**였다(모델이 띄어쓰기·한글숫자를 정리하거나 '맞은편'을
#    붙여 적은 것) — 예: '동천마을 일단지 아파트' → '동천마을 1단지'(정답), '가운마을 2단지,
#    3단지 맞은편' → '가온마을3단지'(정답). 그걸 버리면 맞는 매칭을 잃는다.
_NOT_A_PLACE = re.compile(
    r"(습니다|마세요|않음|없음|생략)\.?$"          # 종결어미·부정으로 끝나는 문장
    r"|규칙에 따라|출력하지|출력 없음"              # 프롬프트 지시문 되풀이
    r"|텍스트에 (없|명시|구체)"                    # "텍스트에 없습니다/명시되지 않음/…"
    r"|명시적으로 (언급|존재)"
)
# 프롬프트가 "라벨 없이 값만"이라고 해도 모델이 라벨을 붙여 내놓는 일이 있다. 두 형태 다 온다:
#   "민원인 위치: 밀익는마을"  → 값만 남긴다
#   "민원인 위치:"            → 값이 없으니 버린다 (실측: 지명 없는 민원에서 라벨 두 줄만 나옴)
_PLACE_LABEL = re.compile(r"^[^:：]{0,20}위치[^:：]{0,10}[:：]\s*")


def extract_places(state: State) -> State:
    sep("STEP 2c | LLM - 냄새 발생 위치 추출")
    raw = llm([{"role": "user", "content": prompts.places_prompt(state["stt_text"])}])
    print(f"[LLM 응답] {raw}")

    candidates, dropped = [], []
    for c in raw.split("\n"):
        c = c.strip()
        if not c or len(c) >= 40:
            continue
        orig = c
        c = _PLACE_LABEL.sub("", c, count=1).strip()   # "민원인 위치: 값" → "값"
        # 괄호로 통째로 감싼 줄은 값이 아니라 주석이다 — "(출력 없음)", "(추정 위치 없음)"
        # 라벨을 떼고 남은 게 없으면(=라벨만 있던 줄) 역시 값이 아니다.
        if (not c or c.endswith(":") or c.endswith("：")
                or (c.startswith("(") and c.endswith(")")) or _NOT_A_PLACE.search(c)):
            dropped.append(orig)
            continue
        c = re.sub(r"(\d),(\d)", r"\1\2", c)
        if c not in candidates:
            candidates.append(c)

    if dropped:
        print(f"  [제외] 지명이 아닌 응답 {len(dropped)}줄: {dropped}")
    print(f"→ 지명 후보: {candidates}")
    return {**state, "place_candidates": candidates}


# 어휘·자모 검색 구현 방식:
#   "sparse"  (기본) 희소행렬 Jaccard — 아래 jaccard와 값이 완전히 동일하고 수십 배 빠르다.
#                    (자모 쪽은 71만 지명의 자모 분해를 색인 구축 때 1회만 하므로 절감폭이 더 크다)
#   "jaccard"        순수 파이썬 기준 구현 (느림). 동치 검증용으로 남겨둔다.
#   "bm25"           어휘 점수 함수만 BM25로 교체 — 기각됨(search_bm25 docstring 참고).
#                    자모는 이 경우에도 Jaccard 희소 구현을 쓴다.
NGRAM_SCORER = os.environ.get("NGRAM_SCORER", "sparse").strip().lower()

# 후보군 구성 전략:
#   "topdown"     (기본) 광역 전체에서 바로 판단 (시군 내 항목은 ★/시군내 태그로 표시)
#   "bottomup"    (기존) 시군 한정으로 먼저 판단 → 실패(best=None)하면 광역으로 확대
#   "subfallback"       광역 전체로 판단 → 실패하면 **시군 한정으로 좁혀 한 번 더** (아래 ■)
#
# 노이즈 800건 전수검사에서 topdown 이 확실히 우세했다 (§7차):
#   후보군 recall (LLM 없이, bench_pool_strategy.py, 후보 834개)
#       시군 한정 0.819  /  광역 전체 0.923  /  시군∪광역 0.930   ← 4개 도 전부 같은 방향
#   실제 F1 (베이스라인 2회 실행 범위 vs topdown)
#       냄새 위치   0.528~0.538 → 0.776   (+0.24, 노이즈의 24배)
#       신고자 위치 0.882~0.884 → 0.893
#       micro       0.835~0.837 → 0.846   /  macro 0.764~0.767 → 0.815
#       냄새 종류·강도·주기(대조군)는 모두 베이스라인 변동 범위 이내 → 부작용 없음
#
# 시군 한정을 먼저 두는 설계는 동명 타지역 오매칭을 막으려는 것이었으나, 실제로는
# subregion_only 가 road_address 문자열 매칭으로 걸러내면서 정답의 10%를 후보군에서
# 잘라내고 있었다 (시군 추정 오류, 발생원이 인접 시군, 주소 표기 불일치).
# 동명 방어 정보는 ★/시군내 태그로 LLM에 그대로 전달되므로 하드 필터가 필요 없다.
# 병합(시군∪광역)은 광역 대비 recall +0.007에 풀 크기 +56%라 채택하지 않았다.
#
# ■ "subfallback" (광역 실패 → 시군 재검색) 은 왜 기본이 아닌가
#   동기: 풀을 좁히면 광역에서 묻혀 있던 정답이 상위로 올라온다. 실측 예 —
#     후보 '방우'(STT가 '광우'를 오인식) 로 검색하면
#       광역 '경상도' 풀 28개: 이방우동·방우회식당·황토방우정모텔 …  → '광우' 없음
#       시군 '포항'   풀 13개: 강우동·광우·금방여우·장우 …          → '광우' 있음 ★
#     (정답 '광우' = 경상북도 포항시 남구 장흥로 131 공장. DB에 실재한다)
#   그런데 800건 전수로 재면 이득이 손실보다 훨씬 작다 (keyword_eval800/bench_sub_fallback.py):
#     대상 = 광역에서 아무것도 못 고른 후보 90개 (전체 후보 1030개의 8.7%)
#       정답이 시군 풀에 있음 → 고칠 기회      6건 (7%)   ← 그중 2건은 후보가 쓰레기라 요행
#       정답 없는데 후보는 있음 → 훼손 위험    82건 (91%)
#       시군 풀이 빔 → 아무 일 없음             2건 (2%)
#   폴백 대상 후보들이 대부분 지명이 아니기 때문이다('하수구', '바다', '집안',
#   '해당 위치가 텍스트에 없습니다' 같은 LLM 출력 쓰레기). 여기에 시군 풀에서 억지로 이름을
#   붙이면, 지금은 원문이 보존되던 자리가 엉뚱한 상호로 바뀐다.
#   유사도 하한을 걸어 안전하게 만들 수 있는지도 재봤지만 두 분포가 완전히 겹친다:
#     정답 이름의 유사도(살려야 하는 값):   0.00 0.07 0.38 0.40 0.57 0.67
#     오답 풀의 최고 유사도(잘라야 하는 값): 중앙 0.33 / 75% 0.50 / 90% 0.60 / 최대 0.80
#     어느 임계를 잡아도 살리는 1건당 훼손 위험이 4~12건이다 (초성일치 기준도 동일).
#   → 그래서 기본은 끄고, 켜려면 POOL_STRATEGY=subfallback 으로 명시한다.
#     제대로 고치는 길은 폴백이 아니라 (a) extract_places 가 문장·일반명사를 후보로 내보내지
#     않게 하는 것, (b) 초성 혼동(ㅂ↔ㄱ 등)을 자모 유사도에 반영하는 것이다.
POOL_STRATEGY = os.environ.get("POOL_STRATEGY", "topdown").strip().lower()


def rag_search(state: State) -> State:
    sep("STEP 3 | RAG 검색 + 통합 매칭 (벡터 + n-gram + 자모 n-gram)"
        + ("" if NGRAM_SCORER == "sparse" else f" [어휘={NGRAM_SCORER}]")
        + ("" if POOL_STRATEGY == "topdown" else f" [풀={POOL_STRATEGY}]"))
    region = state["estimated_region"]
    subregion = state["estimated_subregion"]
    candidates = state["place_candidates"]
    text = state["stt_text"]

    if not candidates:
        print("지명 후보 없음 → 건너뜀")
        return {**state, "vector_results": [], "ngram_results": [], "jamo_results": [],
                "place_matches": [], "corrected_by_place": text}

    print(f"DB: {region} | 시군 참고: {subregion if subregion else '없음'} (필터 아님, 우선순위 태그로만 사용)")
    index, metadata = load_faiss(region)
    if subregion:
        # 참고용 로그. 예전에는 filter_by_subregion()을 불러 708K 리스트를 새로 만든 뒤
        # 반환값을 버렸다(요청마다 GIL 점유). 개수만 필요하므로 마스크 캐시를 쓴다.
        n_sub = subregion_count(region, metadata, subregion)
        print(f"  시군 필터 '{subregion}': {n_sub}건 / 전체 {len(metadata)}건")

    def _lex(queries: list, subregion_only: bool = False) -> list:
        """어휘(n-gram) 검색 — NGRAM_SCORER 토글로 구현/점수함수를 고른다."""
        if NGRAM_SCORER == "jaccard":
            return search_ngram(metadata, queries, top_k=10, subregion=subregion,
                                subregion_only=subregion_only)
        fn = search_bm25 if NGRAM_SCORER == "bm25" else search_ngram_sparse
        return fn(metadata, queries, top_k=10, subregion=subregion,
                  subregion_only=subregion_only, region=region)

    def _jamo(queries: list, subregion_only: bool = False) -> list:
        """자모 n-gram 검색 — 점수 함수는 항상 Jaccard, 구현만 토글."""
        if NGRAM_SCORER == "jaccard":
            return search_jamo(metadata, queries, top_k=10, subregion=subregion,
                               subregion_only=subregion_only)
        return search_jamo_sparse(metadata, queries, top_k=10, subregion=subregion,
                                  subregion_only=subregion_only, region=region)

    def _fmt(candidate: str, r: dict) -> str:
        mark = "★" if r.get("in_subregion") else " "
        name = r.get("place_name", "")
        return f"{mark}{name:<20} 초성 {chosung_score(candidate, name):>4.0%}  {r.get('road_address', '')}"

    def _print_lists(candidate: str, v: list, n: list, j: list) -> None:
        for tag, lst in (("벡터", v), ("n-gram", n), ("자모", j)):
            print(f"  {tag} top{len(lst[:10])}:")
            for i, r in enumerate(lst[:10], 1):
                print(f"    {i:2d}. {_fmt(candidate, r)}")

    def _merge_window(v: list, n: list, j: list, start: int, end: int) -> dict:
        merged = {}
        for tag, lst in (("벡터", v), ("n-gram", n), ("자모", j)):
            for r in lst[start:end]:
                name = r.get("place_name", "")
                if not name:
                    continue
                entry = merged.setdefault(name, {"meta": r, "tags": []})
                if tag not in entry["tags"]:
                    entry["tags"].append(tag)
                if r.get("in_subregion") and "시군내" not in entry["tags"]:
                    entry["tags"].append("시군내")
        return merged

    def _pick_best(candidate: str, v: list, n: list, j: list) -> dict | None:
        for round_no, (start, end) in enumerate(((0, 5), (5, 10)), start=1):
            merged = _merge_window(v, n, j, start, end)
            if not merged:
                continue
            # 후보와 표기가 완전히 같은 장소가 있으면 LLM 판단 없이 확정 (비결정성 제거)
            if candidate in merged:
                return merged[candidate]["meta"]
            # 각 후보에 초성일치(자음 순서 유사도) 점수를 붙여 LLM 판단 근거로 제공
            lines = "\n".join(
                f"- {name} / {info['meta'].get('road_address', '')} "
                f"[{','.join(info['tags'])}] 초성일치 {chosung_score(candidate, name):.0%}"
                for name, info in merged.items()
            )
            raw = llm([{"role": "user", "content": prompts.pick_best_prompt(text, candidate, lines)}], temperature=0)
            picked = raw.strip().split("\n")[0].strip().lstrip("-").strip()
            if picked not in merged:
                # LLM이 "이름 / 주소 [태그]"처럼 목록 라인 전체를 반환하는 경우 이름만 분리
                head = picked.split("/")[0].strip()
                if head in merged:
                    picked = head
                else:  # 응답 문자열 안에 포함된 후보명으로 복구 (가장 긴 이름 우선)
                    contained = [name for name in merged if name and name in picked]
                    if contained:
                        picked = max(contained, key=len)
            if picked in merged:
                return merged[picked]["meta"]
            print(f"  [{candidate}] {round_no}차 통합 후보군에서 매칭 실패({picked}) → 다음 순위 재시도")
        return None

    vec_results, ng_results, jamo_results, place_matches = [], [], [], []
    corrected = text

    # STEP3 검색 시간 분해용 누계(ms). 벡터=GPU임베딩+FAISS, n-gram/자모=CPU scipy.
    _search_ms = {"벡터": 0.0, "n-gram": 0.0, "자모": 0.0}

    def _acc(kind, fn, *a, **k):
        t0 = time.perf_counter()
        r = fn(*a, **k)
        _search_ms[kind] += (time.perf_counter() - t0) * 1000
        return r

    for candidate in candidates:
        # 쿼리 준비 (숫자 표기 변환 시 원본+변환본 둘 다 검색)
        queries = [candidate]
        if re.search(r'\d', candidate) or re.search(r'[일이삼사오육칠팔구십백천만]', candidate):
            raw = llm([{"role": "user", "content": prompts.number_convert_prompt(candidate)}], temperature=0)
            converted = raw.strip().split("\n")[0].strip()
            if converted and converted != candidate:
                queries = [candidate, converted]
                print(f"\n[후보: {candidate}] 숫자 변환 쿼리: {queries}")

        # 검색은 '필요할 때' 한다. 예전에는 광역 전체 검색을 항상 먼저 돌렸는데,
        # 시·군 한정에서 매칭에 성공하면 그 결과는 state 표시용으로만 쓰이고 선택에는
        # 관여하지 않아 통째로 버려졌다 (LLM이 좀처럼 None을 내지 않아 폴백이 드물다).
        # 선택 로직·순서는 그대로이므로 best 와 corrected 는 이전과 완전히 동일하다.
        best = None
        used = None          # 실제 선택이 일어난 풀 → state 저장용
        # 1) 시·군 한정 검색 우선 (정밀 — 동명 타지역 오매칭 방지)
        if subregion and POOL_STRATEGY == "bottomup":
            bv = _acc("벡터", search_vector, index, metadata, queries, top_k=10, subregion=subregion, subregion_only=True)
            bn = _acc("n-gram", _lex, queries, subregion_only=True)
            bj = _acc("자모", _jamo, queries, subregion_only=True)
            print(f"\n[후보: {candidate}] 시·군 '{subregion}' 한정 검색")
            _print_lists(candidate, bv, bn, bj)
            best = _pick_best(candidate, bv, bn, bj)
            used = ("시군", bv, bn, bj)

        # 2) 시·군에서 못 찾으면(또는 시·군 한정을 아예 안 쓰는 전략이면) 광역 전체에서 검색
        if best is None:
            # 주의: 이 안내는 '시군 한정을 실제로 돌려보고 실패한' 경우에만 붙여야 한다.
            #       예전엔 subregion 이 있으면 무조건 붙어서, 시군 검색을 아예 안 한
            #       topdown 실행 로그에도 "시·군 매칭 실패 → 확대"가 찍혀 오해를 샀다.
            note = " (시·군 매칭 실패 → 확대)" if used is not None else ""
            print(f"\n[후보: {candidate}] 광역 '{region}' 전체 검색{note} (★ = 시·군 '{subregion}' 내)")
            v = _acc("벡터", search_vector, index, metadata, queries, top_k=10, subregion=subregion)
            n = _acc("n-gram", _lex, queries)
            j = _acc("자모", _jamo, queries)
            _print_lists(candidate, v, n, j)
            best = _pick_best(candidate, v, n, j)
            used = ("광역", v, n, j)

        # 3) 광역에서도 못 골랐으면 시·군 한정으로 좁혀 한 번 더 (POOL_STRATEGY=subfallback).
        #    풀이 작아지면 광역에서 표기 유사한 타지역 항목에 가려 있던 시군 내 정답이 상위로
        #    올라올 수 있다. 다만 이 폴백은 손실이 이득보다 크게 측정됐다 → 기본은 꺼짐
        #    (수치와 근거는 위 POOL_STRATEGY 주석 ■).
        if best is None and subregion and POOL_STRATEGY == "subfallback":
            print(f"\n[후보: {candidate}] 광역 매칭 실패 → 시·군 '{subregion}' 한정 재검색")
            bv = _acc("벡터", search_vector, index, metadata, queries, top_k=10,
                      subregion=subregion, subregion_only=True)
            bn = _acc("n-gram", _lex, queries, subregion_only=True)
            bj = _acc("자모", _jamo, queries, subregion_only=True)
            _print_lists(candidate, bv, bn, bj)
            best = _pick_best(candidate, bv, bn, bj)
            if best is not None:      # 골랐을 때만 '선택이 일어난 풀'을 시군으로 바꾼다
                used = ("시군", bv, bn, bj)

        # state 에 남기는 것은 '실제 선택이 일어난 풀'이다. 예전에는 항상 광역 목록을
        # 저장해서, 시군 한정에서 선택이 일어난 건의 사후 진단이 불가능했다.
        scope, uv, un, uj = used
        vec_results.append({"candidate": candidate, "scope": scope, "results": uv})
        ng_results.append({"candidate": candidate, "scope": scope, "results": un})
        jamo_results.append({"candidate": candidate, "scope": scope, "results": uj})

        # ── 원본 유지 방어 ──────────────────────────────────
        # STT가 제대로 들은 이름을 RAG가 '보정'하다 오히려 훼손하는 문제가 있었다
        # (노이즈 800건 전수검사: 실패 45건 중 41건이 악화, 그중 18건은 후보가 이미 정답).
        #   예) 후보 '금강 엔지니어링' → '공간엔지니어링',  '경도' → '고도'
        # 기존에도 `candidate in merged` 조기확정이 있었지만 (a) 문자열 완전일치라
        # 공백 하나에 무력화되고 (b) 상위 5~10위 창 안에서만 봐서 대부분 놓쳤다.
        # 여기서는 DB 전체를 공백·기호 무시하고 조회한다.
        # 단, 선택된 이름이 후보를 '포함'하는 확장이면(모아 → 모아엘가더테라스아파트)
        # 그 확장이 맞으므로 건드리지 않는다.
        exact = _db_exact(region, metadata, candidate)
        if exact is not None:
            cand_norm = _norm_place_name(candidate)
            picked_norm = _norm_place_name(best.get("place_name", "")) if best else ""
            is_expansion = bool(picked_norm) and cand_norm in picked_norm and cand_norm != picked_norm
            if not is_expansion and picked_norm != cand_norm:
                dropped = best.get("place_name", "") if best else ""
                print(f"  [{candidate}] DB에 동일 표기 존재 → 보정하지 않고 유지"
                      + (f" (LLM 선택 '{dropped}' 무시)" if dropped else ""))
                best = exact

        best_name = best.get("place_name", "") if best else ""
        print(f"  → [{candidate}] 최종 매칭: {best_name if best_name else '없음'}")
        place_matches.append({
            "candidate": candidate,
            "picked": best_name if best_name else "없음",
            "road_address": best.get("road_address", "") if best else "",
        })
        if best_name:
            corrected = corrected.replace(candidate, best_name)

    print(f"\n→ 통합 보정결과:\n{corrected}")
    print(f"[TIME] STEP3 검색누계: 벡터 {_search_ms['벡터']:.0f}ms / "
          f"n-gram {_search_ms['n-gram']:.0f}ms / 자모 {_search_ms['자모']:.0f}ms "
          f"(나머지 STEP3 시간 = LLM pick_best·숫자변환)")

    return {**state,
            "vector_results": vec_results, "ngram_results": ng_results, "jamo_results": jamo_results,
            "place_matches": place_matches, "corrected_by_place": corrected}


def correct_remaining(state: State) -> State:
    sep("STEP 4 | RAG few-shot - 잔여 오인식 보정")
    text = state["corrected_by_place"]
    region = state["estimated_region"]

    # 환각/반복 제거는 STEP1.5(remove_noise)에서 이미 끝났으므로 여기선 재수행하지 않는다.
    # (STEP1.5가 stt_text를 청소해뒀고, 이후 STEP2~3도 그 텍스트를 이어받아 corrected_by_place도 이미 깨끗함)

    # few-shot 개수: 예시가 많을수록(5) 모델이 예시의 '사투리 어투'를 따라 원문에 없던 방언을
    # 생성하는 경향이 커진다. 3개로 줄여 보정 신호는 유지하되 어투 전이(over-generation)를 낮춘다.
    #
    # 정답 유출 주의: 데이터셋 음성으로 시연·평가하면 그 음성의 (오인식, 정답) 쌍이 DB 에
    # 그대로 들어 있어서 1위로 뽑히고, 모델이 정답지를 베낀다(실측: 경상도_id243 의 '광우'가
    # STT 에서 '방우'로 들렸는데 정답 예시를 보고 '광우'로 복원됨 — 실력이 아니다).
    # 그래서 search_stt_err 가 유사도 임계(STT_ERR_LEAK_SIM) 이상인 예시를 빼낸다.
    # 어떤 예시가 실제로 들어갔는지 로그로 남긴다 — 다시 조용히 유출되면 안 된다.
    examples = search_stt_err(text, region, top_k=3)
    print(f"유사 오인식 예시 {len(examples)}개 검색됨 (지역: {region})")
    for i, ex in enumerate(examples, 1):
        print(f"  예시{i} 유사도 {ex.get('score', 0):.4f}  id={ex.get('id', '?')}")

    few_shot_lines = []
    for i, ex in enumerate(examples, 1):
        few_shot_lines.append(f"예시{i})\n오인식: {ex['stt']}\n정상:   {ex['original']}")
    few_shot_block = "\n\n".join(few_shot_lines)

    protected_rule = prompts.protected_names_rule(
        _protected_names(state), default="- 이미 보정된 지명은 그대로 유지",
    )
    prompt = prompts.correct_remaining_prompt(few_shot_block, protected_rule, text)

    raw = llm([{"role": "user", "content": prompt}])
    corrected = raw.strip()

    # 화자 분리(상담원/민원인 라벨) → 이후 단계·화면이 공유할 대화 형태로 확정
    corrected_final = _to_dialogue(corrected)

    print(f"\n→ 최종 보정결과(화자분리):\n{corrected_final}")
    return {"corrected_final": corrected_final}


def normalize_dialect(state: State) -> State:
    sep("STEP 5 | LLM - 사투리 → 표준어 변환")
    text = state["corrected_final"]   # 이미 화자분리된 대화 형태

    protected_rule = prompts.protected_names_rule(_protected_names(state))
    if protected_rule:
        protected_rule += "\n"
    prompt = prompts.normalize_prompt(protected_rule, text, region=state.get("estimated_region", ""))

    raw = llm([{"role": "user", "content": prompt}])
    # 보정본과 동일하게 발화 사이 빈 줄 유지 → 화면 간격·파랑 diff 정렬 일치
    normalized = "\n\n".join(ln for ln in raw.strip().split("\n") if ln.strip())

    print(f"\n→ 표준어 변환결과:\n{normalized}")
    return {"normalized_text": normalized}


def extract_keywords(state: State) -> State:
    sep("STEP 6 | 키워드 추출 (Qwen fine-tuned)")
    decoded = kw_llm(prompts.keywords_v1_prompt(state["normalized_text"]))
    decoded = re.sub(r"<think>.*?</think>", "", decoded, flags=re.DOTALL).strip()

    print(f"\n→ 키워드:\n{decoded}")
    return {"keywords": decoded}


def _kw_lines(prompt: str, n: int) -> list:
    """키워드 프롬프트를 돌려 n줄을 받아온다. 줄 수가 모자라면 '미언급'으로 채운다."""
    raw = llm([{"role": "user", "content": prompt}], temperature=0)
    decoded = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    lines = [ln.strip() for ln in decoded.split("\n") if ln.strip()][:n]
    return lines + ["미언급"] * (n - len(lines))


# 키워드 프롬프트 방식: True = 위치/냄새 분리(LLM 2회), False = 5항목 통합(LLM 1회).
# 환경변수 KEYWORDS_SPLIT_PROMPT=0/1 로 바꿀 수 있다(어블레이션 측정용).
#
# 기본값 False(통합). 노이즈 800건 어블레이션에서 분리가 더 나빴다:
#   micro F1 통합 0.837 vs 분리 0.832, 냄새 강도 0.772 vs 0.743.
# 5항목이 한 프롬프트에 있으면 "다른 항목에 쓴 내용은 여기 쓰지 말라"는 규칙이 서로를
# 억제하는데, 냄새 3항목만 떼어내면 그 억제가 사라져 과잉생성이 늘었다(강도 104→131).
# LLM 호출도 2배라 지연만 늘고 얻는 게 없다. 분리 프롬프트는 재측정용으로 남겨둔다.
KEYWORDS_SPLIT_PROMPT = os.environ.get("KEYWORDS_SPLIT_PROMPT", "0") == "1"


def _too_similar(a: str, b: str, thr: float = 0.6) -> bool:
    """두 슬롯 값이 사실상 같은 문장인지. 부분포함 + 문자 유사도 둘 다 본다."""
    a, b = a.strip(), b.strip()
    if not a or not b or a == "미언급" or b == "미언급":
        return False
    ca, cb = a.replace(" ", ""), b.replace(" ", "")
    if ca in cb or cb in ca:            # 한쪽이 다른 쪽을 통째로 포함
        return True
    return difflib.SequenceMatcher(None, ca, cb).ratio() >= thr


def extract_keywords_v2(state: State) -> State:
    """STEP 6 | 키워드 추출. 출력 계약(keywords = KW_KEYS dict 문자열)은 두 방식 모두 동일."""
    matched_places = _protected_names(state)  # STEP3에서 확정된 정식 지명 → 그대로 쓰도록 전달
    text = state["normalized_text"]

    if KEYWORDS_SPLIT_PROMPT:
        sep("STEP 6 | LLM - 키워드 추출 (v2, 위치/냄새 분리)")
        place = _kw_lines(prompts.keywords_place_prompt(text, matched_places), 2)
        odor = _kw_lines(prompts.keywords_odor_prompt(text), 3)
        # KW_KEYS 순서: 신고자 위치, 냄새 종류, 냄새 강도, 냄새 주기, 냄새 위치
        values = [place[0], odor[0], odor[1], odor[2], place[1]]
    else:
        sep("STEP 6 | LLM - 키워드 추출 (v2, 5항목 통합)")
        values = _kw_lines(prompts.keywords_v2_unified_prompt(text, matched_places), 5)

    # --- 중복 슬롯 정리 (프롬프트 227~228 규칙을 코드로 강제) ---
    # values = [신고자위치, 냄새종류, 강도, 주기, 냄새위치]  (아직 전부 str)
    if _too_similar(values[2], values[3]):   # 강도·주기 겹침 → 강도가 이긴다
        values[3] = "미언급"
    if values[4].strip() and values[4].strip() == values[0].strip():  # 냄새위치=신고자위치
        values[4] = "미언급"

    values[1] = [v.strip() for v in values[1].split(",") if v.strip()]  # 냄새 종류는 목록
    keywords = str(dict(zip(KW_KEYS, values)))

    print(f"\n→ 키워드(v2):\n{keywords}")
    return {"keywords": keywords}
