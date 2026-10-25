# PB수첩 글로벌 속보 — 华尔街见闻 실시간 속보 한국어판

[华尔街见闻 7×24 快讯](https://wallstreetcn.com/live/global)을 주기적으로 받아와 **한국어로 번역해 실시간으로 보여주는 Flask 웹앱**입니다.
화면 상단에는 **주요 지수 6종(S&P 500 · NASDAQ 100 · NIKKEI 225 · 상해종합(SHANGHAI) · HSCEI · KOSPI) + 원달러 환율 + 금(COMEX 선물)** 실시간 시세와 **최근 6개월 추이 차트**가 표시됩니다.

## 주요 기능
- **실시간 속보**: 서버가 10초마다 원본을 수집하고, 화면은 5초마다 *변경분만* 받아와 반영 (새 속보는 잠깐 하이라이트)
- **시세 바(8종)**: 현재가 · 전일 대비 · 등락률 · 거래소 현지 시각(`LAST | 2:12:10 PM JST`) · **6개월 추이 차트와 6개월 수익률**
  - PC·태블릿은 4칸 × 2줄, 휴대폰은 옆으로 넘겨 보는 한 줄
  - **상승 = 빨강, 하락 = 초록**, 장 마감 후에는 `CLOSE` 표시, 값이 바뀌면 깜빡임 효과
  - 1차 Yahoo Finance → 실패 시 네이버 증권으로 자동 전환(지수), 모두 실패하면 마지막 값을 흐리게 유지
  - 시세(가격·등락)는 15초마다 갱신, **6개월 차트는 15분마다 갱신(화면에 "15분 지연" 표시)**
- **중요 뉴스만 보기**, 키워드 검색(한글·중국어 동시, 하이라이트), 복사 · 출처 링크
- **유튜브 라이브 방송**: 시세창과 속보 사이에 표시(음소거 자동재생, 접기/펼치기 기억, 접으면 연결 해제). 영상은 `YOUTUBE_LIVE_ID`로 변경, 빈 값이면 숨김
- **최근 48시간** 속보만 표시(`RETENTION_HOURS`로 변경 가능)
- 중국 A주/홍콩 개별 종목 공시성 뉴스 자동 제외
- 다크/라이트 테마, 모바일 대응, 마지막 설정 기억

## 폴더 구조
```
pb_wire_kr/
├── run.py                  # 실행 진입점 (waitress)
├── config.py               # 설정 (환경변수로 재정의)
├── requirements.txt
├── .env.example            # 환경변수 예시 (복사해서 .env 로 사용)
├── render.yaml / Procfile / Dockerfile
├── README.md
├── app/
│   ├── __init__.py         # 앱 팩토리, 보안 헤더, 수집 스레드 시작
│   ├── routes.py           # /  ·  /api/news  ·  /api/market  ·  /api/market/history  ·  /api/status
│   ├── services/
│   │   ├── feed.py         # 속보 수집 · 신규분 번역 · 재시도(백오프) · 변경분 제공
│   │   ├── translator.py   # 다중 번역 엔진 + 결과 검증 + 서킷 브레이커 + LRU 캐시
│   │   └── market.py       # 지수·환율·금 시세 + 6개월 추이(Yahoo → 네이버 폴백)
│   ├── templates/index.html
│   └── static/
│       ├── css/style.css
│       └── js/app.js
└── tests/                  # pytest (네트워크 없이 실행)
```

## 설치 · 실행
```bash
python -m venv venv
# Windows: venv\Scripts\activate   /   macOS·Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env        # macOS·Linux: cp .env.example .env
python run.py
```
브라우저에서 **http://127.0.0.1:5000** 접속.
수집 스레드가 프로세스마다 생기므로 **워커 1개(멀티 스레드)** 로 운영하세요.

## 번역이 중국어 원문으로 나오던 문제 — 원인과 수정 내용
| 원인 | 수정 |
|---|---|
| Render 등 **클라우드 서버 IP는 Google/Bing 무료 번역이 차단**(429·CAPTCHA 페이지)되는 경우가 많음 | Microsoft **Edge 번역 엔드포인트** 추가(클라우드 IP에서도 잘 동작), 공식 API(DeepL·Azure) 지원 |
| 차단 페이지·원문 그대로·MyMemory 쿼터 경고문 같은 **"가짜 성공" 결과가 그대로 저장·캐시**됨 | 결과에 한글이 있고 한자가 남지 않았는지 **검증**, 실패 시 다음 엔진으로 넘기고 캐시하지 않음 |
| MyMemory는 **500자 제한**, Google GET은 URL 길이 제한 → 긴 본문은 항상 실패 | 문장 단위로 **450자 이하 분할 번역** 후 결합, 조각별 캐시 |
| 엔진이 막혀도 매번 타임아웃까지 기다려 **번역 큐가 수 분씩 정체** | 엔진별 **서킷 브레이커**(실패 시 15초→최대 10분 건너뜀) |
| 10회 실패하면 **영구히 원문 상태**로 남음 | 항목별 **지수 백오프 재시도**(5초→최대 5분, 최대 40회) |
| 서버 재시작(Render 무료 플랜 슬립) 후 화면이 **번역 전 DOM을 계속 재사용** | 데이터 객체가 바뀌면 항목을 다시 그리도록 수정 |

### 가장 확실한 방법: 공식 번역 API 키 등록 (무료)
무료 엔드포인트는 언제든 차단될 수 있습니다. 아래 중 하나를 Render의 **Environment** 에 등록하면 가장 안정적입니다.

| 엔진 | 환경변수 | 무료 한도 |
|---|---|---|
| **DeepL API Free** (추천, 금융 문장 품질 우수) | `DEEPL_API_KEY` (`:fx`로 끝나는 키) | 월 50만 자 |
| **Azure AI Translator** (F0) | `AZURE_TRANSLATOR_KEY`, `AZURE_TRANSLATOR_REGION` (예: `koreacentral`) | 월 200만 자 |
| Papago (NCP) | `PAPAGO_CLIENT_ID`, `PAPAGO_CLIENT_SECRET` | 유료 |
| MyMemory | `MYMEMORY_EMAIL` (이메일만 넣으면 일 5천→5만 자) | 일 5만 자 |

### DeepL 번역 품질 설정 (자동 적용)
- 본문을 자르지 않고 **전체를 한 번에** 번역(문맥 유지)
- 제목은 본문을, 본문은 제목을 **문맥(context)** 으로 함께 전달 — context는 사용량에 포함되지 않음
- "중국 금융 속보 · 한국어 기사체 · 주요 용어(美联储=연준, 降息=금리 인하 등)" 배경 정보 제공
- 고품질 모델(`prefer_quality_optimized`) 우선 사용, 미지원 시 자동 해제
- 월 한도 소진(456) 시 1시간 쉬고 그동안 무료 엔진으로 폴백

엔진 우선순위(auto): DeepL → Azure → Papago → Edge → Google → Bing → MyMemory
(`TRANSLATOR=edge` 처럼 지정하면 그 엔진이 맨 앞으로 오고 나머지는 자동 폴백)

## 하루하나 글 추가하기
글 목록은 `app/data/haruhana.json` 한 파일로 관리합니다. **맨 위 글이 화면 맨 앞**에 나옵니다.
```json
{
  "title": "글 제목",
  "date": "2026-10-26",
  "summary": "한두 줄 요약(비워도 됨)",
  "url": "https://...notion.site/... 또는 https://www.notion.so/..."
},
```
- `url`은 **Notion 주소만** 허용(그 외 주소는 자동으로 무시), `date`는 `YYYY-MM-DD` 형식 또는 빈 값
- Notion 글은 **공유 → 웹에 게시**를 켜야 다른 사람도 볼 수 있음
- GitHub에서 파일을 수정·커밋하면 Render가 자동 재배포
- 주소 끝에 `#haruhana`를 붙이면 하루하나 탭이 바로 열림

## 환경변수
| 이름 | 기본값 | 설명 |
|---|---|---|
| `POLL_INTERVAL` | 10 | 속보 수집 주기(초) |
| `RETENTION_HOURS` | 48 | 표시·보관할 속보 기간(시간) |
| `YOUTUBE_LIVE_ID` | VlaCnp1fxI8 | 라이브 방송 유튜브 영상 ID(11자) |
| `MARKET_POLL_INTERVAL` | 15 | 지수 시세 수집 주기(초) |
| `MAX_ITEMS_PER_CHANNEL` | 1000 | 서버 보관 건수 상한 |
| `TRANSLATE_WORKERS` | 4 | 병렬 번역 스레드 수 |
| `TRANSLATION_CACHE_SIZE` | 3000 | 번역 캐시 상한 |
| `HTTP_TIMEOUT` | 8 | 외부 요청 타임아웃(초) |

## 성능 · 메모리
- 원본·시세는 **서버 1곳에서만** 수집 → 사용자가 늘어도 외부 호출 수는 그대로
- 지수는 6개를 **병렬 수집**, 최근 2분 내 조회가 없으면 수집 중지(불필요한 호출 0)
- **신규 항목만 번역**(병렬), 조각 단위 LRU 캐시, 실패 엔진은 즉시 건너뜀
- 보관 건수·번역 캐시 **상한** → 오래 켜 두어도 메모리가 늘지 않음
- 화면은 변경분만 수신, DOM 재사용, 탭이 백그라운드면 속보·시세 폴링 모두 중지, 오류 시 지수 백오프

## 보안
- 원본 텍스트는 **순수 텍스트만** 사용, 화면에서도 `textContent`로만 출력 (XSS 차단)
- 시세 데이터도 숫자 포맷 후 `textContent`로만 출력
- CSP · X-Frame-Options · nosniff · Referrer-Policy 보안 헤더, 쿼리 파라미터 화이트리스트 검증
- API 키는 환경변수에서만 읽고 화면·로그·상태 API에 노출하지 않음

## 확인 · 문제 해결
- 상태 확인: `/api/status` — 엔진별 성공/실패 횟수와 쿨다운, 미번역 건수, 마지막 수집 시각
  - 모든 무료 엔진의 `fail`이 계속 늘어나면 서버 IP가 차단된 상태 → **DeepL/Azure 키 등록**
- 시세 확인: `/api/market`, 6개월 추이: `/api/market/history`
- 테스트: `pip install pytest && pytest` (실제 엔진 호출 테스트는 `RUN_NETWORK_TESTS=1`)

## 유의사항
- 华尔街见闻 공개 웹 API와 Yahoo/네이버 시세를 사용하므로 원본 측 사정으로 형식이 바뀌거나 접근이 제한될 수 있습니다.
- 지수 시세는 거래소/제공처 정책에 따라 지연될 수 있습니다.
- 기계번역 결과입니다. 내부 참고용으로 사용하시고, 외부 배포·재게시는 원본 저작권을 확인하세요.
