"""
search.py
- FAISS 인덱스 로드 및 검색 함수
- load_faiss        : 지역별 지명 FAISS 인덱스 로드 (지역별 캐시)
- warmup_regions    : 기동 시 FAISS + 희소색인 예열 (첫 요청 지연 제거)
- filter_by_subregion: 시·군 기준 메타데이터 필터링
- search_vector     : 의미 벡터 유사도 검색
- search_ngram      : 글자 n-gram Jaccard 유사도 검색 (기준 구현, 순수 파이썬)
- search_ngram_sparse: 위와 '값이 동일'한 희소행렬 구현 (수십 배 빠름, GIL 해제)
- search_bm25       : 글자 n-gram BM25 검색 (대체 실험용 — 기각, 함수 docstring 참고)
- search_jamo       : 자모 분해 n-gram 유사도 검색 (기준 구현, 순수 파이썬)
- search_jamo_sparse: 위와 '값이 동일'한 희소행렬 구현
- load_stt_err_faiss: 지역별 오인식-정상 쌍 FAISS 인덱스 로드 (lazy, 지역별 캐시)
- search_stt_err    : 지역에 맞는 오인식 예시 top-K 검색 (few-shot용)
"""
import json
import os
import threading
import time
import numpy as np
import scipy.sparse as sp
import faiss
from core.config import FAISS_DIR, STT_ERR_FAISS, REGIONS, get_embedder
from core.text_utils import (ngram_set, decompose_jamo,
                             ngram_score_pre, jamo_score_pre)

# 코어 수가 매우 많은 서버에서 faiss 기본 OpenMP 스레드 수(=전체 코어)로 대형 flat
# 인덱스를 검색하면 BLAS가 "too many memory regions" 오류로 죽는 문제가 있어 제한한다.
faiss.omp_set_num_threads(4)


# --- FAISS GPU 이관 (선택) --------------------------------------------------
# 대형 flat 인덱스(경상도 IndexFlatIP 708k×768)의 CPU 브루트포스가 지연의 큰 축이다.
# faiss-gpu 가 깔려 있고 GPU 가 보이면 인덱스를 GPU 로 올린다. 아니면 조용히 CPU 로 둔다
# — faiss-cpu 이거나 로그인노드(GPU 없음)에서 import 해도 안전하게 CPU 로 동작한다.
#   FAISS_GPU=0        → 강제로 CPU
#   FAISS_GPU_DEVICE=N → 올릴 GPU (기본 1. cuda:0 은 Whisper+임베더가 있어 여유가 ~3.5GB뿐)
#   FAISS_GPU_FP16=0   → float32 로 올림 (기본 float16: VRAM 절반, flat 랭킹 정확도 영향 미미)
_GPU_ENABLED = os.environ.get("FAISS_GPU", "1") != "0"
_GPU_DEVICE = int(os.environ.get("FAISS_GPU_DEVICE", "1"))
_GPU_FP16 = os.environ.get("FAISS_GPU_FP16", "1") != "0"
_gpu_res = None  # StandardGpuResources 는 인덱스가 사는 동안 살아있어야 한다 (GC 금지)


def _to_gpu(index):
    """가능하면 인덱스를 GPU 로 옮겨 돌려준다. 불가하면 원본(CPU)을 그대로 돌려준다."""
    global _gpu_res
    if not _GPU_ENABLED:
        return index
    try:
        ngpu = faiss.get_num_gpus()
        if ngpu <= 0 or not hasattr(faiss, "StandardGpuResources"):
            return index  # faiss-cpu 이거나 GPU 안보임 → CPU 유지
        dev = _GPU_DEVICE if _GPU_DEVICE < ngpu else 0  # 장수 부족하면 0 으로
        if _gpu_res is None:
            _gpu_res = faiss.StandardGpuResources()
        opts = faiss.GpuClonerOptions()
        opts.useFloat16 = _GPU_FP16
        gpu_index = faiss.index_cpu_to_gpu(_gpu_res, dev, index, opts)
        print(f"[faiss] 인덱스를 GPU:{dev} 로 이관 (fp16={_GPU_FP16}, ntotal={index.ntotal})", flush=True)
        return gpu_index
    except Exception as e:
        print(f"[faiss] GPU 이관 실패 → CPU 로 진행: {e}", flush=True)
        return index


_faiss_cache: dict = {}
_faiss_locks: dict = {}
_faiss_locks_guard = threading.Lock()


def _region_lock(region: str) -> threading.Lock:
    """지역별 로드 락. 전역 락 하나로 두면 경상도(4.2초)를 읽는 동안
    강원도(0.1초) 요청까지 막힌다."""
    with _faiss_locks_guard:
        return _faiss_locks.setdefault(region, threading.Lock())


def load_faiss(region: str):
    """지역별 지명 FAISS 인덱스 + metadata 로드 (지역당 1회, 이후 재사용).

    캐시 필수: 경상도는 index.faiss 2.0GB + metadata 708,878건이라 1회 로드에
    4.2초 / 3.1GB가 든다. 캐시가 없던 시절에는 이걸 **민원 요청마다 다시 읽었다.**
    5개 지역(REGIONS)을 모두 올려도 약 4.3GB로, warmup_regions() 로 기동 시 예열한다.

    FAISS 인덱스는 읽기 전용 검색만 하므로 스레드 간 공유해도 안전하다.
    DB(index.faiss/metadata.jsonl)를 재생성했다면 프로세스를 재시작해야 한다
    (_lex_index / nodes._db_name_index 도 같은 전제)."""
    cached = _faiss_cache.get(region)
    if cached is not None:
        return cached
    with _region_lock(region):
        cached = _faiss_cache.get(region)          # 락 획득 후 재확인
        if cached is not None:
            return cached
        region_dir = FAISS_DIR / region
        index = _to_gpu(faiss.read_index(str(region_dir / "index.faiss")))
        metadata = []
        with open(region_dir / "metadata.jsonl", encoding="utf-8") as f:
            for line in f:
                metadata.append(json.loads(line))
        _faiss_cache[region] = (index, metadata)
        return index, metadata


def warmup_regions(regions: list = None) -> None:
    """기동 시 지역별 FAISS + 어휘/자모 희소색인을 미리 만들어 첫 요청 지연을 없앤다.

    예열 없이도 동작은 같다(첫 요청이 대신 비용을 낸다). 경상도의 경우 그 첫 요청이
    load_faiss 4.2초 + 희소색인 8.3초를 물기 때문에 미리 해두는 편이 낫다.
    한 지역이 실패해도 나머지는 계속 예열한다 — 실패한 지역은 lazy 경로로 재시도된다."""
    for region in (regions or REGIONS):
        t0 = time.perf_counter()
        try:
            _, metadata = load_faiss(region)
            for kind in ("ngram", "jamo"):
                _lex_index(region, metadata, kind)
            print(f"  [warmup] {region}: 문서 {len(metadata):,} "
                  f"({time.perf_counter() - t0:.1f}s)")
        except Exception as e:
            print(f"  [warmup][WARN] {region} 예열 실패(첫 요청 시 재시도): {e}")


def filter_by_subregion(metadata: list, subregion: str) -> list:
    """시·군에 해당하는 metadata 부분집합.

    주의: 로그를 찍을 목적으로 이 함수를 부르면 안 된다. 708,878건(경상도) 리스트를
    새로 만들며 GIL을 잡는데, 호출부가 반환값을 버리면 그 비용이 전부 낭비다
    (실제로 rag_search가 매 요청 그렇게 하고 있었다). 개수만 필요하면
    subregion_count() 를 쓸 것 — 검색이 쓰는 마스크 캐시를 재사용한다."""
    if not subregion:
        return metadata
    filtered = [
        item for item in metadata
        if subregion in item.get("road_address", "")
    ]
    return filtered if filtered else metadata


def subregion_count(region: str, metadata: list, subregion: str) -> int:
    """시·군 내 문서 수. search_*_sparse 가 쓰는 마스크 캐시를 그대로 재사용하므로
    같은 (지역, 시군)에 대해 두 번째 호출부터는 O(1)이다."""
    if not subregion:
        return len(metadata)
    return int(_subregion_mask(region, metadata, subregion).sum())


def search_vector(index, metadata: list, queries: list, top_k: int = 10,
                  subregion: str = "", subregion_only: bool = False) -> list:
    """지역 전체 사전 구축 FAISS 인덱스로 검색 (재임베딩 없음). 냄새 발생 추정 장소는
    신고자와 다른 시·군에 있을 수 있으므로 subregion으로 결과를 제외하거나 순위를
    왜곡하지 않고, 각 결과에 in_subregion 태그만 붙여 참고 정보로 전달한다.
    subregion_only=True면 시·군 내 결과만 남긴다 (광역 매칭 실패 시 재검색용)."""
    if not queries or index is None or not metadata:
        return []

    query_vecs = get_embedder().encode(queries, normalize_embeddings=True).astype(np.float32)
    search_k = min(max(top_k * 20, 200), index.ntotal)
    distances, indices = index.search(query_vecs, search_k)

    # 쿼리(원본+숫자변환 등)가 여러 개일 수 있으므로 place_name별 최고 점수를 모아
    # 전체를 한 번에 정렬한다 (쿼리별로 따로 top_k를 뽑아 이어붙이면, 뒤 쿼리의 더 좋은
    # 매칭이 순위에 못 들어오는 문제가 있었음).
    scored = {}
    for dist_row, idx_row in zip(distances, indices):
        for dist, idx in zip(dist_row, idx_row):
            if idx == -1 or idx >= len(metadata):
                continue
            item = metadata[idx]
            name = item.get("place_name", "")
            if not name:
                continue
            in_subregion = bool(subregion) and subregion in item.get("road_address", "")
            if subregion_only and not in_subregion:
                continue
            score = float(dist)
            if name not in scored or scored[name]["vector_score"] < score:
                scored[name] = {**item, "vector_score": score, "in_subregion": in_subregion}

    results = sorted(scored.values(), key=lambda x: x["vector_score"], reverse=True)
    return results[:top_k]


def search_ngram(metadata: list, queries: list, top_k: int = 10,
                 subregion: str = "", subregion_only: bool = False) -> list:
    if not queries:
        return []
    scored = {}
    for query in queries:
        q_set = ngram_set(query)          # 쿼리 n-gram은 루프 밖에서 1회만 (결과 동일)
        for item in metadata:
            place = item.get("place_name", "")
            in_subregion = bool(subregion) and subregion in item.get("road_address", "")
            if subregion_only and not in_subregion:
                continue
            score = ngram_score_pre(q_set, place)
            if score > 0:
                key = place
                if key not in scored or scored[key]["ngram_score"] < score:
                    scored[key] = {**item, "ngram_score": score, "in_subregion": in_subregion}
    results = sorted(scored.values(), key=lambda x: x["ngram_score"], reverse=True)
    return results[:top_k]


# ── 어휘(문자 n-gram) 희소 색인 ─────────────────────────────────────────────
# search_ngram 은 쿼리 하나마다 지역 전체(경상도 71만건)를 순수 파이썬으로 훑는다.
# 이는 (a) 느리고 (b) GIL을 계속 붙잡아 ThreadPool 병렬화를 무력화한다.
# 아래 색인은 '문서 × n-gram' 이진 희소행렬을 지역당 1회 만들어 두고, 검색을
# 열 슬라이스 + 합/행렬곱 1회로 바꾼다. 같은 색인으로 두 점수 함수를 모두 낸다:
#
#   Jaccard : |q∩d| / (|q| + |d| - |q∩d|)          ← search_ngram 과 값이 완전히 동일
#   BM25    : w(|d|) · Σ_{g∈q∩d} idf[g]
#
# BM25가 저 형태로 접히는 이유: 자질이 집합이라 tf=1 이므로
# (tf·(k1+1))/(tf + k1·(1-b+b·|d|/avgdl)) 가 문서마다 상수 w(|d|) 가 된다.
_BM25_K1 = 1.5
_BM25_B = 0.75

_lex_cache: dict = {}
_lex_lock = threading.Lock()
_submask_cache: dict = {}
_submask_lock = threading.Lock()


def _doc_grams(kind: str, name: str) -> set:
    """문서 측 자질. jamo 는 '자모 분해 후 n-gram' 이며, 이 분해를 색인 구축 때
    1회만 하는 것이 search_jamo 대비 이득의 큰 부분이다 (기존에는 후보 검색마다
    71만 개 지명을 매번 다시 분해했다)."""
    return ngram_set(decompose_jamo(name)) if kind == "jamo" else ngram_set(name)


def _query_grams(kind: str, query: str) -> set:
    """쿼리 측 자질. jamo_score_pre 가 받는 q_jamo_set 과 정확히 같은 정의."""
    return ngram_set(decompose_jamo(query)) if kind == "jamo" else ngram_set(query)


def _lex_index(region: str, metadata: list, kind: str = "ngram"):
    """지역별 어휘 희소 색인을 1회 구축 후 재사용. kind: "ngram" | "jamo".
    반환: (vocab, 이진 CSC 행렬, 문서별 자질 개수 dl, idf, BM25 길이가중 w)

    락 필수: 배치 처리에서 여러 스레드가 같은 지역에 동시에 도달하면 각자
    71만 건(경상도) 색인을 중복 구축하며 GIL을 붙잡아 전체가 멈춘다.

    전제: 캐시 키가 region 뿐이므로 행렬의 i번째 행이 항상 metadata[i] 여야 한다.
    load_faiss 는 요청마다 metadata 리스트를 새로 만들지만 metadata.jsonl 을 순서대로
    읽으므로 내용·순서가 동일해 안전하다. FAISS DB를 재생성했다면 프로세스를 재시작해
    이 캐시를 비워야 한다 (nodes._db_name_index 도 같은 전제)."""
    key = (region, kind)
    idx = _lex_cache.get(key)
    if idx is not None:
        return idx
    with _lex_lock:
        idx = _lex_cache.get(key)              # 락 획득 후 재확인
        if idx is not None:
            return idx
        vocab: dict = {}
        indices, indptr, doc_len = [], [0], []
        for item in metadata:
            n_before = len(indices)
            for gram in _doc_grams(kind, item.get("place_name", "")):
                col = vocab.get(gram)
                if col is None:
                    col = len(vocab)
                    vocab[gram] = col
                indices.append(col)
            doc_len.append(len(indices) - n_before)
            indptr.append(len(indices))

        n_docs, n_vocab = len(metadata), max(len(vocab), 1)
        ind = np.asarray(indices, dtype=np.int32)
        # 이진 행렬 (자질이 집합이므로 값은 전부 1). Jaccard의 교집합 크기를
        # 정수로 정확히 얻기 위해 int32 로 둔다 — 부동소수 누적 오차 없음.
        mat = sp.csr_matrix(
            (np.ones(ind.size, dtype=np.int32), ind, np.asarray(indptr, dtype=np.int64)),
            shape=(n_docs, n_vocab), dtype=np.int32,
        ).tocsc()

        dl = np.asarray(doc_len, dtype=np.int64)          # |d| (n-gram 집합 크기)
        # 자질이 집합이라 문서당 각 n-gram이 1회뿐 → 열별 등장 횟수 = df
        df = np.bincount(ind, minlength=n_vocab).astype(np.float64)
        idf = np.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
        avgdl = float(dl.mean()) if n_docs and dl.sum() else 1.0
        w = (_BM25_K1 + 1.0) / (1.0 + _BM25_K1 *
                                (1.0 - _BM25_B + _BM25_B * dl / avgdl))

        idx = (vocab, mat, dl, idf, w)
        _lex_cache[key] = idx
        print(f"  [{kind}색인] {region}: 문서 {n_docs:,} / 자질 {len(vocab):,}")
        return idx


def _subregion_mask(region: str, metadata: list, subregion: str) -> np.ndarray:
    """road_address에 시·군명이 있는지를 문서별로 미리 계산해 캐시한다.
    시·군 수는 유한하고 한 요청 안에서 후보마다 재사용되므로 캐시가 잘 맞는다."""
    key = (region, subregion)
    mask = _submask_cache.get(key)
    if mask is not None:
        return mask
    with _submask_lock:
        mask = _submask_cache.get(key)
        if mask is not None:
            return mask
        mask = np.fromiter(
            (subregion in (item.get("road_address") or "") for item in metadata),
            dtype=bool, count=len(metadata),
        )
        _submask_cache[key] = mask
        return mask


def _lex_scores(region: str, metadata: list, queries: list, scorer: str,
                kind: str = "ngram") -> np.ndarray:
    """쿼리별 점수를 계산해 문서별 최고점 배열을 돌려준다.
    여러 쿼리가 있으면 place_name별 최고점을 쓴다 (search_ngram과 같은 규칙)."""
    vocab, mat, dl, idf, w = _lex_index(region, metadata, kind)
    scores = np.zeros(mat.shape[0], dtype=np.float64)
    for query in queries:
        q_set = _query_grams(kind, query)
        if not q_set:
            continue                       # search_ngram도 이 경우 전부 0점
        cols = np.asarray(sorted({vocab[g] for g in q_set if g in vocab}), dtype=np.int32)
        if cols.size == 0:
            continue
        # 교집합 크기 |q∩d| (정수). 열 슬라이스라 훑는 양이 해당 n-gram 보유 문서로 한정된다.
        inter = np.asarray(mat[:, cols].sum(axis=1)).ravel()
        if scorer == "bm25":
            s = w * np.asarray(mat[:, cols] @ idf[cols]).ravel()
        else:
            # |q| 는 vocab에 없는 n-gram까지 포함한 '쿼리 전체' 크기여야 한다.
            # 그래야 합집합이 ngram_score_pre 와 정확히 같아진다.
            denom = len(q_set) + dl - inter
            s = np.divide(inter, denom, out=np.zeros_like(scores),
                          where=denom > 0)
        np.maximum(scores, s, out=scores)
    return scores


def _lex_topk(region: str, metadata: list, scores: np.ndarray, top_k: int,
              subregion: str, subregion_only: bool,
              score_key: str = "ngram_score") -> list:
    """점수 배열에서 place_name 중복을 제거하고 상위 top_k를 뽑는다.
    search_ngram의 관행을 그대로 재현한다 — 점수 내림차순, 동점은 metadata 등장 순서,
    같은 이름은 첫 등장이 대표(같은 이름은 n-gram 집합·길이가 같아 점수도 같다)."""
    hit = scores > 0
    if subregion_only:
        if not subregion:
            return []
        hit = hit & _subregion_mask(region, metadata, subregion)
    order = np.flatnonzero(hit)
    if order.size == 0:
        return []

    def collect(idxs):
        results, seen = [], set()
        for i in idxs:
            name = metadata[i].get("place_name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            in_sub = bool(subregion) and subregion in (metadata[i].get("road_address") or "")
            results.append({**metadata[i], score_key: float(scores[i]),
                            "in_subregion": in_sub})
            if len(results) >= top_k:
                break
        return results

    # 이름 중복 제거 때문에 top_k보다 넉넉히 남긴 뒤 정렬한다.
    cap = max(top_k * 200, 2000)
    if order.size > cap:
        head = np.sort(order[np.argpartition(-scores[order], cap)[:cap]])
        got = collect(head[np.argsort(-scores[head], kind="stable")])
        if len(got) >= top_k:
            return got
        # 잘라낸 구간에서만 top_k를 못 채운 희귀한 경우 → 전체로 다시 (결과 동일 보장)
    return collect(order[np.argsort(-scores[order], kind="stable")])


def search_ngram_sparse(metadata: list, queries: list, top_k: int = 10,
                        subregion: str = "", subregion_only: bool = False,
                        region: str = "") -> list:
    """search_ngram 과 '값이 완전히 동일한' 희소행렬 구현 (점수 함수 안 바뀜).

    교집합을 int32 행렬 합으로 정수로 구하고 int/int 나눗셈을 하므로
    ngram_score_pre 의 len(q&t)/len(q|t) 와 비트 단위로 같은 float64가 나온다.
    scipy 연산은 GIL을 놓으므로 ThreadPool 병렬화도 실제로 효과가 생긴다."""
    if not queries or not metadata:
        return []
    scores = _lex_scores(region, metadata, queries, "jaccard", "ngram")
    return _lex_topk(region, metadata, scores, top_k, subregion, subregion_only,
                     "ngram_score")


def search_jamo_sparse(metadata: list, queries: list, top_k: int = 10,
                       subregion: str = "", subregion_only: bool = False,
                       region: str = "") -> list:
    """search_jamo 와 '값이 완전히 동일한' 희소행렬 구현.

    search_jamo 는 후보 검색마다 71만 개 지명을 전부 자모 분해했다
    (jamo_score_pre 가 매번 decompose_jamo(target) 호출). 여기서는 그 분해를
    색인 구축 때 1회만 하므로 ngram 쪽보다 절감폭이 더 크다."""
    if not queries or not metadata:
        return []
    scores = _lex_scores(region, metadata, queries, "jaccard", "jamo")
    return _lex_topk(region, metadata, scores, top_k, subregion, subregion_only,
                     "jamo_score")


def search_bm25(metadata: list, queries: list, top_k: int = 10,
                subregion: str = "", subregion_only: bool = False,
                region: str = "") -> list:
    """어휘 검색을 BM25로 대체하는 실험용 (기각됨 — 아래 근거).

    노이즈 800건 어블레이션: 자질을 고정하고 점수만 바꿨을 때 정답 지명 868개 중
    단 1개만 결과가 달라졌다(전체 풀 recall@10 0.789 → 0.790).
    이유는 실제 지명 코퍼스의 IDF 격차가 작기 때문이다 — '아파'(df 9%) idf 2.41,
    희귀 n-gram idf 9.12 로 최대 4.7배뿐이라, 후보가 정답과 공유하는 n-gram 개수
    효과를 못 뒤집는다. 게다가 Jaccard의 분모 |q∪d| 가 BM25(b=0.75)보다 강한 길이
    정규화라 짧은 고유명사 매칭에는 오히려 더 적합하다.
    반환 dict의 점수 키는 'ngram_score' 그대로 둬 하위 노드를 건드리지 않는다."""
    if not queries or not metadata:
        return []
    scores = _lex_scores(region, metadata, queries, "bm25", "ngram")
    return _lex_topk(region, metadata, scores, top_k, subregion, subregion_only,
                     "ngram_score")


def search_jamo(metadata: list, queries: list, top_k: int = 10,
                subregion: str = "", subregion_only: bool = False) -> list:
    if not queries:
        return []
    scored = {}
    for query in queries:
        # 쿼리 자모 분해 + n-gram도 루프 밖에서 1회만 (결과 동일)
        q_jamo_set = ngram_set(decompose_jamo(query))
        for item in metadata:
            place = item.get("place_name", "")
            in_subregion = bool(subregion) and subregion in item.get("road_address", "")
            if subregion_only and not in_subregion:
                continue
            score = jamo_score_pre(q_jamo_set, place)
            if score > 0:
                key = place
                if key not in scored or scored[key]["jamo_score"] < score:
                    scored[key] = {**item, "jamo_score": score, "in_subregion": in_subregion}
    results = sorted(scored.values(), key=lambda x: x["jamo_score"], reverse=True)
    return results[:top_k]


_stt_err_cache = {}


def load_stt_err_faiss(region: str):
    if region in _stt_err_cache:
        return _stt_err_cache[region]
    region_dir = STT_ERR_FAISS / region
    index = _to_gpu(faiss.read_index(str(region_dir / "index.faiss")))
    meta = []
    with open(region_dir / "metadata.jsonl", encoding="utf-8") as f:
        for line in f:
            meta.append(json.loads(line))
    _stt_err_cache[region] = (index, meta)
    return index, meta


# 정답 유출(leakage) 차단 임계. 인덱스는 IndexFlatIP + 정규화 임베딩이라 score = 코사인
# 유사도(1에 가까울수록 같은 문서)다. 이보다 유사한 예시는 **입력 자신**(같은 녹음의
# 오인식-정답 쌍)으로 보고 few-shot 에서 빼낸다.
#   왜 필요한가: 데이터셋 음성으로 시연·평가하면 그 음성의 (오인식, 정답) 쌍이 이 DB 에
#   그대로 들어 있다. 그러면 모델이 정답지를 보고 베끼므로 보정 성능이 실제보다 좋게 나온다.
#   실측(경상도_id243): 1위 0.9916 = 입력 자신(정답에 '광우' 포함), 2위 0.8741 → 확실히 갈린다.
# 실서비스 입력은 DB 에 없으니 이 가드가 발동하지 않는다(=성능 손실 없음).
_LEAK_SIM = float(os.environ.get("STT_ERR_LEAK_SIM", "0.97"))


def search_stt_err(text: str, region: str, top_k: int = 5) -> list:
    index, meta = load_stt_err_faiss(region)
    vec = get_embedder().encode([text], normalize_embeddings=True).astype(np.float32)
    # 자기 자신이 걸러질 수 있으니 넉넉히 받아 와서 top_k 개를 채운다
    distances, indices = index.search(vec, min(top_k + 3, index.ntotal))
    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx == -1 or idx >= len(meta):
            continue
        score = float(dist)
        if score >= _LEAK_SIM:
            print(f"[RAG] few-shot 제외: 입력과 동일 추정 (유사도 {score:.4f} ≥ {_LEAK_SIM}) "
                  f"id={meta[idx].get('id', '?')}", flush=True)
            continue
        results.append({**meta[idx], "score": score})
        if len(results) >= top_k:
            break
    return results
