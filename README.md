# server_piai — 코드 백업

악취 민원 사투리 STT 보정 파이프라인. **코드와 소형 인덱스만** 담은 백업이다.

## 이 저장소에 없는 것

| 대상 | 이유 |
|---|---|
| `kong/.secrets.env` · 모든 토큰·키 | 비밀값. 배포 UI 환경변수로 주입한다 |
| `A/data/complaints_raw/` | 실제 민원 통화 녹음·전사·연락처·주소 (개인정보) |
| `B/data/*.db` · `data/*.db` | 실제 접수 DB |
| `kong/logs/` | 로그에 민원 전문이 평문으로 남는다 |
| `model/` (162GB) · `A/models/` (423MB) | 용량. `A/models` 는 HuggingFace 에서 재다운로드 |
| `dataset/faiss/location/` (3.2GB) | 100MB 초과 파일 6개 → git 한도 초과. 별도 보관 |
| `dataset/voice_saturi/` (9.7GB) · `dataset/location/` (296MB) | 용량 |

`dataset/faiss/stt_err/` (6.8MB) 만 포함했다.

## 복원

1. 이 저장소를 받는다.
2. `dataset/faiss/location/` 과 `model/`, `A/models/` 를 별도 보관처에서 가져온다.
3. `pip install -r server/server_piai/requirements.txt -c server/server_piai/constraints.txt`
4. 아래 환경변수를 배포 UI 또는 셸에 넣는다.
5. `python server/server_piai/run_server.py`

## 필요한 환경변수

값은 이 저장소에 없다. 없으면 그 기능만 꺼지고 나머지는 동작한다.

| 변수 | 없으면 |
|---|---|
| `UI_USER` · `UI_PASS` | 자동 생성 (UI 로그인) |
| `GATEWAY_TOKEN` | 게이트웨이 헤더 검사 OFF |
| `LLM_API_KEY` | vLLM 키 검사 OFF |
| `LIVE_PROGRESS_TOKEN` | 진행상황 API 헤더 검사 OFF |
| `NAVER_CLIENT_ID` · `NAVER_CLIENT_SECRET` | 장소 이름 검색 OFF |
| `NCP_GEOCODE_KEY_ID` · `NCP_GEOCODE_KEY` | 주소→좌표 변환 · UI 지도 OFF |
| `PARTNER_WEBHOOK_URL` · `PARTNER_WEBHOOK_TOKEN` | 파트너 웹훅 전송 OFF |
| `EXTERNAL_BASE_URL` | UI 화면이 안 뜬다 (API 는 동작) |

## 주의

주석에서 실제 IP·호스트·비밀번호·파트너 주소를 제거했다. 운영 세부사항은
운영 환경의 원본을 볼 것.
