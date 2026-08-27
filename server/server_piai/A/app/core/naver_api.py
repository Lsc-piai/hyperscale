# app/core/naver_api.py  (서버 A)
"""
네이버 위치 조회 (민원 발생지 → 위경도). 두 종류의 API를 함께 쓴다.
- 지역(Local) 검색 API (openapi.naver.com): 상호·아파트·랜드마크 등 '장소 이름'으로 검색.
  → naver_local_search. mapx/mapy = WGS84 * 1e7 정수.
- 지오코딩 API (NCP Maps): '도로명/지번 주소'를 좌표로 변환 (이름은 못 찾고 주소 전용).
  → geocode_address. 응답의 x=경도, y=위도.
- 키는 **환경변수로만** 받는다. 코드에 기본값을 두지 않는다 — 예전에는 실키가
  하드코딩돼 있어서, 이 파일을 열 수 있는 누구나 우리 API 할당량을 쓸 수 있었다.
  run_server.py 가 kong/.secrets.env(또는 배포 UI env)에서 읽어 넣어준다.
  키가 없으면 예외를 던지지 않고 좌표 없이 진행한다(경고 1회) — 민원 접수 자체는
  좌표가 없어도 되므로, 키 하나 때문에 파이프라인을 세우지 않는다.
"""
import html
import json
import os
import re
import urllib.parse
import urllib.request

# 지역 검색 API 키 (장소 이름 검색용)
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
LOCAL_SEARCH_URL = "https://openapi.naver.com/v1/search/local.json"

# NCP Maps 지오코딩 API 키 (도로명 주소 → 좌표용)
NCP_GEOCODE_KEY_ID = os.environ.get("NCP_GEOCODE_KEY_ID", "")
NCP_GEOCODE_KEY = os.environ.get("NCP_GEOCODE_KEY", "")
GEOCODE_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"

# 키가 없을 때 경고를 한 번만 찍는다 (민원 한 건마다 반복되면 로그를 덮는다).
_warned = set()


def _has_key(which: str) -> bool:
    """which: 'local'(지역검색) | 'geocode'(지오코딩). 키가 없으면 False + 경고 1회."""
    if which == "local":
        ok = bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)
        names = "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET"
    else:
        ok = bool(NCP_GEOCODE_KEY_ID and NCP_GEOCODE_KEY)
        names = "NCP_GEOCODE_KEY_ID / NCP_GEOCODE_KEY"
    if not ok and which not in _warned:
        _warned.add(which)
        print(f"[naver_api][WARN] {names} 가 없다 → 좌표 변환을 건너뛴다. "
              f"kong/.secrets.env 또는 배포 UI 환경변수에 넣을 것.", flush=True)
    return ok


def geocode_address(address: str) -> tuple:
    """도로명/지번 주소 → (경도, 위도). 실패 시 (None, None). (NCP Maps 지오코딩)
    상호명 등 '이름'은 못 찾고 주소만 처리한다."""
    a = (address or "").strip()
    if not a:
        return None, None
    if not _has_key("geocode"):
        # 긴 안내는 _has_key 가 프로세스당 1회만 찍는다. 여기서는 **호출마다** 짧게 남긴다 —
        # 안 그러면 두 번째 지명부터 조용히 건너뛰어서, 도로명 폴백이 돌았는지조차
        # 로그에서 알 수 없다 (실제로 그래서 "도로명 검색이 어디 갔냐"를 추적해야 했다).
        print(f"[GEOCODE] 건너뜀(NCP 키 없음): '{a}'", flush=True)
        return None, None
    url = f"{GEOCODE_URL}?{urllib.parse.urlencode({'query': a})}"
    req = urllib.request.Request(url, headers={
        "x-ncp-apigw-api-key-id": NCP_GEOCODE_KEY_ID,
        "x-ncp-apigw-api-key": NCP_GEOCODE_KEY,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[GEOCODE][ERROR] 조회 실패 ({a}): {e}")
        return None, None
    addrs = data.get("addresses", [])
    if not addrs:
        print(f"[GEOCODE] 결과 없음: '{a}'")
        return None, None
    top = addrs[0]
    lon, lat = float(top["x"]), float(top["y"])   # x=경도, y=위도
    print(f"[GEOCODE] '{a}' → {top.get('roadAddress', '')} (위도 {lat}, 경도 {lon})")
    return lon, lat


def naver_local_search(query: str, display: int = 5) -> list:
    """지역 검색. 반환: [{"title", "road_address", "address", "lat", "lon"}, ...]"""
    if not _has_key("local"):
        return []
    url = f"{LOCAL_SEARCH_URL}?{urllib.parse.urlencode({'query': query, 'display': display})}"
    req = urllib.request.Request(url, headers={
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results = []
    for item in data.get("items", []):
        title = html.unescape(re.sub(r"</?b>", "", item.get("title", "")))
        mapx, mapy = item.get("mapx"), item.get("mapy")
        results.append({
            "title": title,
            "road_address": item.get("roadAddress", ""),
            "address": item.get("address", ""),
            "lon": int(mapx) / 1e7 if mapx else None,
            "lat": int(mapy) / 1e7 if mapy else None,
        })
    return results
