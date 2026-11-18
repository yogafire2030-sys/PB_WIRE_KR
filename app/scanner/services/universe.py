"""스크리닝 대상 종목(S&P 500 + 나스닥 100).

- 1순위: 위키백과 구성 종목 표를 하루 1회 조회(편입·편출 자동 반영)
- 2순위(조회 실패 시): 아래 내장 목록 사용
티커는 Yahoo 형식으로 정규화(BRK.B → BRK-B)하고 형식 검증 후에만 사용합니다.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:-[A-Z]{1,2})?$")

# 내장 목록(위키백과 조회 실패 시 사용)
_SP500 = """
A AAPL ABBV ABNB ABT ACGL ACN ADBE ADI ADM ADP ADSK AEE AEP AES AFL AIG AIZ AJG AKAM ALB ALGN ALL ALLE AMAT
AMCR AMD AME AMGN AMP AMT AMZN ANET AON AOS APA APD APH APO APP APTV ARE ATO AVB AVGO AVY AWK AXON AXP AZO
BA BAC BALL BAX BBY BDX BEN BF-B BG BIIB BK BKNG BKR BLDR BLK BMY BR BRK-B BRO BSX BX BXP C CAG CAH CARR CAT
CB CBOE CBRE CCI CCL CDNS CDW CEG CF CFG CHD CHRW CHTR CI CINF CL CLX CMCSA CME CMG CMI CMS CNC CNP COF COIN
COO COP COR COST CPAY CPB CPRT CPT CRL CRM CRWD CSCO CSGP CSX CTAS CTRA CTSH CTVA CVS CVX D DAL DASH DAY DD
DDOG DE DECK DELL DG DGX DHI DHR DIS DLR DLTR DOC DOV DOW DPZ DRI DTE DUK DVA DVN DXCM EA EBAY ECL ED EFX EG
EIX EL ELV EME EMN EMR EOG EPAM EQIX EQR EQT ERIE ES ESS ETN ETR EVRG EW EXC EXE EXPD EXPE EXR F FANG FAST FCX
FDS FDX FE FFIV FI FICO FIS FITB FOX FOXA FRT FSLR FTNT FTV GD GDDY GE GEHC GEN GEV GILD GIS GL GLW GM GNRC
GOOG GOOGL GPC GPN GRMN GS GWW HAL HAS HBAN HCA HD HIG HII HLT HOLX HON HOOD HPE HPQ HRL HSIC HST HSY HUBB
HUM HWM IBKR IBM ICE IDXX IEX IFF INCY INTC INTU INVH IP IPG IQV IR IRM ISRG IT ITW IVZ J JBHT JBL JCI JKHY
JNJ JPM K KDP KEY KEYS KHC KIM KKR KLAC KMB KMI KMX KO KR KVUE L LDOS LEN LH LHX LII LIN LKQ LLY LMT LNT LOW
LRCX LULU LUV LVS LW LYB LYV MA MAA MAR MAS MCD MCHP MCK MCO MDLZ MDT MET META MGM MHK MKC MLM MMC MMM MNST
MO MOH MOS MPC MPWR MRK MRNA MS MSCI MSFT MSI MTB MTCH MTD MU NCLH NDAQ NDSN NEE NEM NFLX NI NKE NOC NOW NRG
NSC NTAP NTRS NUE NVDA NVR NWS NWSA NXPI O ODFL OKE OMC ON ORCL ORLY OTIS OXY PANW PAYC PAYX PCAR PCG PEG PEP
PFE PFG PG PGR PH PHM PKG PLD PLTR PM PNC PNR PNW PODD POOL PPG PPL PRU PSA PSX PTC PWR PYPL QCOM RCL REG
REGN RF RJF RL RMD ROK ROL ROP ROST RSG RTX RVTY SBAC SBUX SCHW SHW SJM SLB SMCI SNA SNPS SO SOLV SPG SPGI
SRE STE STLD STT STX STZ SW SWK SWKS SYF SYK SYY T TAP TDG TDY TECH TEL TER TFC TGT TJX TKO TMO TMUS TPL TPR
TRGP TRMB TROW TRV TSCO TSLA TSN TT TTD TTWO TXN TXT TYL UAL UBER UDR UHS ULTA UNH UNP UPS URI USB V VICI VLO
VLTO VMC VRSK VRSN VRTX VST VTR VTRS VZ WAB WAT WBD WDAY WDC WEC WELL WFC WM WMB WMT WRB WSM WST WTW WY WYNN
XEL XOM XYL XYZ YUM ZBH ZBRA ZTS
"""
_NDX = """
AAPL ABNB ADBE ADI ADP ADSK AEP AMAT AMD AMGN AMZN APP ARM ASML AVGO AXON AZN BIIB BKNG BKR CCEP CDNS CDW CEG
CHTR CMCSA COST CPRT CRWD CSCO CSGP CSX CTAS CTSH DASH DDOG DXCM EA EXC FANG FAST FTNT GEHC GFS GILD GOOG GOOGL
HON IDXX INTC INTU ISRG KDP KHC KLAC LIN LRCX LULU MAR MCHP MDLZ MELI META MNST MRVL MSFT MSTR MU NFLX NVDA
NXPI ODFL ON ORLY PANW PAYX PCAR PDD PEP PLTR PYPL QCOM REGN ROP ROST SBUX SHOP SNPS TEAM TMUS TSLA TTD TTWO
TXN VRSK VRTX WBD WDAY XEL ZS
"""


def normalize(sym: str) -> str:
    s = (sym or "").strip().upper().replace(".", "-")
    return s if _TICKER_RE.match(s) else ""


def _parse_list(text: str) -> list[str]:
    return sorted({t for t in (normalize(x) for x in text.split()) if t})


BUNDLED = {"sp500": _parse_list(_SP500), "ndx": _parse_list(_NDX)}


def parse_wiki_table(html: str, min_count: int) -> list[str]:
    """위키백과 구성 종목 표에서 'Symbol'/'Ticker' 열을 찾아 티커 목록 반환."""
    soup = BeautifulSoup(html, "lxml")
    tables = soup.select("table#constituents") or soup.select("table.wikitable")
    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue
        heads = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        col = next((i for i, h in enumerate(heads) if h in ("symbol", "ticker")), None)
        if col is None:
            continue
        out = set()
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) > col:
                t = normalize(cells[col].get_text(" ", strip=True))
                if t:
                    out.add(t)
        if len(out) >= min_count:  # 엉뚱한 표를 잘못 읽은 경우 방지
            return sorted(out)
    return []


def fetch(session, timeout) -> dict:
    """{"sp500": [...], "ndx": [...], "source": "wikipedia"|"bundled"} — 실패한 쪽은 내장 목록 사용."""
    result, source = {}, "wikipedia"
    for key, url, min_count in (("sp500", SP500_URL, 450), ("ndx", NDX_URL, 90)):
        tickers = []
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                tickers = parse_wiki_table(r.text, min_count)
            r.close()
        except Exception as e:
            log.warning("구성 종목 조회 실패[%s]: %s", key, type(e).__name__)
        if not tickers:
            tickers, source = BUNDLED[key], "bundled"
        result[key] = tickers
    result["source"] = source
    return result
