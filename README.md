# Global Wire KR — 华尔街见闻 실시간 속보 한국어판

[华尔街见闻 7×24 快讯](https://wallstreetcn.com/live/global)을 주기적으로 받아와 **한국어로 번역해 실시간으로 보여주는 Flask 웹앱**입니다.

## 주요 기능
- **실시간 갱신**: 서버가 10초마다 원본을 수집하고, 화면은 5초마다 *새로 들어온 것만* 받아와 위에 추가 (새 속보는 잠깐 하이라이트)
- **채널 탭**: 주요뉴스(要闻) · 중국 A주 · 미국주식 · 홍콩주식 · 외환 · 원자재 · 채권
- **중요 뉴스만 보기**: 원본 사이트의 '只看重要的'와 같은 기준(빨간색 속보)
- **중국어 원문 병기** 토글, 항목별 원문 보기 · 복사 · 출처 링크
- **키워드 검색**(한글·중국어 동시, 하이라이트), 날짜별 구분선, 한국시간(KST) 표기
- 다크/라이트 테마, 모바일 대응, 마지막 설정 기억

## 폴더 구조
```
wscn_live_kr/
├── run.py                  # 실행 진입점
├── config.py               # 설정 (환경변수로 재정의)
├── requirements.txt
├── .env.example
├── README.md
└── app/
    ├── __init__.py         # 앱 팩토리, 보안 헤더, 수집 스레드 시작
    ├── routes.py           # /  ·  /api/news  ·  /api/status
    ├── services/
    │   ├── feed.py         # 수집 · 신규분 번역 · 보관(상한) · 변경분 제공
    │   └── translator.py   # DeepL / Papago / Google + LRU 캐시
    ├── templates/index.html
    └── static/
        ├── css/style.css
        └── js/app.js
```

## 설치 · 실행
```bash
cd wscn_live_kr
python -m venv venv
# Windows: venv\Scripts\activate   /   macOS·Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env        # macOS·Linux: cp .env.example .env
python run.py
```
브라우저에서 **http://127.0.0.1:5000** 접속.

사내 다른 PC에서도 보려면 `.env`에서 `HOST=0.0.0.0`으로 바꾸세요.
운영용 실행: `waitress-serve --host=0.0.0.0 --port=5000 --threads=8 run:app`
(수집 스레드가 프로세스마다 생기므로 **워커 1개(멀티 스레드)** 로 운영하세요.)

## 번역 엔진
| 설정 | 내용 |
|---|---|
| `TRANSLATOR=auto` (기본) | DeepL 키 → Papago 키 → Google 순서. 실패하면 다음 엔진으로 자동 전환 |
| Google | 키 없이 바로 동작하는 무료 엔드포인트. 비공식이라 호출이 많으면 일시 차단될 수 있음 |
| DeepL | `DEEPL_API_KEY` 입력 (무료 키 `:fx`로 끝나면 자동으로 free 서버 사용). 금융 문장 품질 가장 안정적 |
| Papago | 네이버 클라우드 플랫폼 Papago Translation의 `PAPAGO_CLIENT_ID / SECRET` |

번역이 실패한 항목은 원문과 함께 **'번역 대기'** 표시가 붙고, 다음 수집 때 최대 3회 자동 재시도합니다.

## 성능 · 메모리
- 원본은 **서버 1곳에서만** 수집 → 사용자가 늘어도 원본/번역 호출 수는 그대로
- **신규 항목만 번역**(병렬), 같은 문장은 LRU 캐시로 재사용
- 아무도 보지 않는 채널은 3분 뒤 수집 중지 (주요뉴스는 항상 수집)
- 채널별 보관 300건, 번역 캐시 3,000건 **상한** → 오래 켜 두어도 메모리가 늘지 않음
- 화면은 `since` 커서로 **변경분만** 수신, 항목 DOM 재사용, 탭이 백그라운드면 폴링 중지, 오류 시 지수 백오프

## 보안
- 원본 텍스트는 HTML이 아닌 **순수 텍스트(content_text)** 만 사용하고, 화면에서도 `textContent`로만 출력 (XSS 차단)
- CSP · X-Frame-Options · nosniff · Referrer-Policy 등 보안 헤더 적용, 인라인 스크립트 없음
- 쿼리 파라미터 화이트리스트/형식 검증, 오류 응답에 내부 정보 미노출
- API 키는 `.env`에서만 읽고 화면·로그에 노출하지 않음 (`.env`는 공유 금지)

## 확인 · 문제 해결
- 상태 확인: `http://127.0.0.1:5000/api/status` (엔진, 채널별 보관 건수, 마지막 수집 시각, 오류)
- 화면 상단 표시등: 초록=정상, 빨강=원본 지연/재연결 중, 회색=탭 비활성으로 일시정지
- 특정 채널이 계속 비어 있으면 원본 채널 ID가 바뀐 것일 수 있습니다 → `config.py`의 `CHANNELS` 값 수정
- 원본 API 주소가 바뀌면 `.env`에 `WSCN_API_URL=` 로 지정

## 유의사항
- 华尔街见闻 공개 웹 API를 사용하므로 원본 측 사정으로 형식이 바뀌거나 접근이 제한될 수 있습니다.
- 기계번역 결과입니다. 내부 참고용으로 사용하시고, 외부 배포·재게시는 원본 저작권을 확인하세요.
