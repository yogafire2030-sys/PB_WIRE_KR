"""하루 한 번 실행용 CLI (작업 스케줄러 / cron 등록용).

예)  python run_daily.py                # 전체 지수, 저장된 설정으로 갱신
     python run_daily.py --target KOSPI
     python run_daily.py --demo         # 오프라인 데모 데이터
"""
from __future__ import annotations

import argparse
import logging
import sys

import config
from core import pipeline, store
from core.data import DataError


def main() -> int:
    ap = argparse.ArgumentParser(description="Factor Factory 일일 업데이트")
    ap.add_argument("--target", choices=["all", *config.TARGETS], default="all")
    ap.add_argument("--demo", action="store_true", help="합성 데모 데이터 사용")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    keys = list(config.TARGETS) if args.target == "all" else [args.target]
    store.init_db()
    try:
        done = pipeline.run_targets([(k, pipeline.saved_settings(k)) for k in keys], demo=args.demo)
    except (DataError, ValueError) as exc:
        logging.error("실패: %s", exc)
        return 1
    logging.info("완료: %s", ", ".join(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
