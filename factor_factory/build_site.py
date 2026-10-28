"""정적 웹사이트 생성기 — GitHub Actions에서 매일 실행.

1) 4개 지수 데이터 수집 → 롤링 모델 + N주 전망 계산 (로컬 앱과 동일 엔진)
2) Flask 템플릿을 그대로 렌더링해 HTML로 저장 → GitHub Pages에 배포

예)  python build_site.py --out _site
     python build_site.py --out _site --demo      # 오프라인 확인용
"""
from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
from pathlib import Path

import config
from core import pipeline, report, store
from core.data import DataError

PAGES = {"KOSPI": "index.html", "KOSDAQ": "kosdaq.html", "SPX": "spx.html", "NDX": "ndx.html"}


def render(out: Path) -> int:
    from app import app  # 템플릿·필터 재사용

    links = {k: PAGES.get(k, f"{k.lower()}.html") for k in config.TARGETS}
    written = 0
    with app.app_context():
        tmpl = app.jinja_env.get_template("index.html")
        for key in config.TARGETS:
            data = report.dashboard(key)
            if not data:
                logging.warning("%s 결과가 없어 페이지를 건너뜁니다.", key)
                continue
            csv_name = f"factor_{key}.csv"
            html = tmpl.render(
                data=data, target=key, targets=config.TARGETS,
                settings=pipeline.saved_settings(key).to_dict(), csrf="",
                demo=data["source"] == "demo", static_site=True, static_prefix="static/",
                links=links, csv_href=csv_name, archive_href="archive.html")
            (out / links[key]).write_text(html, encoding="utf-8")
            with open(out / csv_name, "w", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh).writerows(report.today_csv_rows(key))
            written += 1
        atmpl = app.jinja_env.get_template("archive.html")
        upd = next((report.dashboard(k) or {}).get("updated_at", "") for k in config.TARGETS)
        (out / "archive.html").write_text(atmpl.render(
            a=report.archive(), targets=config.TARGETS, static_site=True, static_prefix="static/",
            links=links, archive_href="archive.html", target=None, updated=upd), encoding="utf-8")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="Factor Factory 정적 사이트 생성")
    ap.add_argument("--out", default="_site")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    store.init_db()
    try:
        jobs = [(k, pipeline.saved_settings(k)) for k in config.TARGETS]
        pipeline.run_targets(jobs, demo=args.demo)
    except (DataError, ValueError) as exc:
        logging.error("계산 실패: %s", exc)
        return 1

    n = render(out)
    if n == 0:
        logging.error("생성된 페이지가 없습니다.")
        return 1
    shutil.copytree(config.BASE_DIR / "static", out / "static")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    logging.info("사이트 생성 완료: %d개 페이지 → %s", n, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
