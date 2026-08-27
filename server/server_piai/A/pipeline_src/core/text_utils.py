"""
text_utils.py
- 텍스트 유사도 및 변환 유틸리티
- decompose_jamo: 한글 문자를 초·중·종성으로 분해
- ngram_set     : 문자열의 n-gram 집합 생성
- ngram_score   : 두 문자열 간 n-gram Jaccard 유사도
- jamo_score    : 자모 분해 후 n-gram Jaccard 유사도
- chosung_only  : 초성(첫 자음)만 추출
- chosung_score : 초성열 위치 정렬 유사도 (모음 무시, 같은 자리 자음 일치 기준)
"""
from core.config import CHOSUNG, JUNGSUNG, JONGSUNG


def decompose_jamo(text: str) -> str:
    result = []
    for char in text:
        if '가' <= char <= '힣':
            code = ord(char) - 0xAC00
            cho  = code // (21 * 28)
            jung = (code % (21 * 28)) // 28
            jong = code % 28
            result.append(CHOSUNG[cho])
            result.append(JUNGSUNG[jung])
            if jong:
                result.append(JONGSUNG[jong])
        else:
            result.append(char)
    return ''.join(result)


def ngram_set(text: str, n: int = 2) -> set:
    text = text.replace(" ", "")
    return {text[i:i+n] for i in range(len(text) - n + 1)}


def ngram_score_pre(q_set: set, target: str, n: int = 2) -> float:
    """쿼리 n-gram 집합을 미리 만들어 두고 쓰는 버전.

    search_ngram/search_jamo 는 쿼리 하나로 지역 전체(경상도 71만건)를 훑는데,
    ngram_score 를 그대로 쓰면 '똑같은 쿼리'의 n-gram 집합과 자모 분해를 71만 번
    다시 만든다. 쿼리 측 계산을 루프 밖으로 빼기 위한 함수이며, 결과는 ngram_score 와
    완전히 동일하다(계산 순서만 다름)."""
    t_set = ngram_set(target, n)
    if not q_set or not t_set:
        return 0.0
    return len(q_set & t_set) / len(q_set | t_set)


def ngram_score(query: str, target: str, n: int = 2) -> float:
    return ngram_score_pre(ngram_set(query, n), target, n)


def jamo_score_pre(q_jamo_set: set, target: str, n: int = 2) -> float:
    """자모 버전. q_jamo_set = ngram_set(decompose_jamo(query), n) 을 미리 넘긴다."""
    return ngram_score_pre(q_jamo_set, decompose_jamo(target), n)


def jamo_score(query: str, target: str, n: int = 2) -> float:
    return jamo_score_pre(ngram_set(decompose_jamo(query), n), target, n)


def chosung_only(text: str) -> str:
    out = []
    for char in text:
        if '가' <= char <= '힣':
            out.append(CHOSUNG[(ord(char) - 0xAC00) // (21 * 28)])
    return ''.join(out)


def chosung_score(query: str, target: str) -> float:
    """초성(첫 자음)만 뽑아, 두 초성열을 한 칸씩 밀어가며(offset 허용) 같은 자리 자음이
    가장 많이 겹치는 정렬을 찾아 그 일치 비율을 낸다. 분모는 더 긴 쪽 길이. 0~1.
    - 모음을 무시하므로 '고양이'~'거양이'처럼 모음만 다른 오인식에 강하다.
    - 순서·연속성(자음이 몇 번째에 오는지)을 반영하되, 앞뒤에 군더더기 글자가 붙어
      통째로 어긋나는 경우를 막는다. 예: '짱 나홍테크'의 앞 '짱' 때문에 밀려도
      '나홍테크'↔'나웅테크' 정렬을 찾아 높은 점수를 준다."""
    q, t = chosung_only(query), chosung_only(target)
    if not q or not t:
        return 0.0
    n, m = len(q), len(t)
    best = 0
    # t를 q 위에서 좌우로 밀어가며(offset = q인덱스 - t인덱스) 겹치는 구간의 일치 최대치
    for off in range(-(m - 1), n):
        matches = sum(
            1 for j in range(m)
            if 0 <= off + j < n and q[off + j] == t[j]
        )
        if matches > best:
            best = matches
    return best / max(n, m)
