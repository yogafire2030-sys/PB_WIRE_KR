"""수집 → 팩터 생성 → 롤링 회귀 → 저장 파이프라인 (웹·CLI 공용)."""
from __future__ import annotations

import gc
import logging
import time
from typing import Callable, Iterable

import config
from core.data import DataError
from core import data, factory, forecast, model, report, store

log = logging.getLogger(__name__)


def run_targets(jobs: Iterable[tuple[str, model.Settings]], demo: bool = False,
                progress: Callable[[str], None] | None = None) -> list[str]:
    jobs = [(k, s) for k, s in jobs if k in config.TARGETS]
    say = progress or (lambda msg: log.info(msg))
    store.init_db()

    say("데이터 수집 중…")
    t0 = time.perf_counter()
    raw = data.load_raw(demo=demo, refresh=True)  # 여러 지수도 한 번만 수집
    log.info("수집 완료 %.1fs (source=%s)", time.perf_counter() - t0, raw["source"])

    done = []
    for key, s in jobs:
        name = config.TARGETS[key]["name"]
        say(f"{name} 팩터 생성·회귀 중…")
        t1 = time.perf_counter()
        y, X = factory.build(raw, key, s.mode)
        stats_df, fac_df = model.run(y, X, s)
        store.save_run(key, s.to_dict(), raw["source"], stats_df, fac_df)
        log.info("%s 완료 %.1fs (%d일 × %d팩터)", name, time.perf_counter() - t1,
                 len(stats_df), X.shape[1])
        del y, X, stats_df, fac_df

        say(f"{name} 다음 {s.horizon}거래일 전망 계산 중…")
        try:
            t2 = time.perf_counter()
            yf, Xf = forecast.build_features(raw, key, s.horizon)
            fc = forecast.run(yf, Xf, s)
            store.save_forecast(key, report.forecast_payload(key, fc, s, Xf.index[-1]))
            log.info("%s 전망 완료 %.1fs (피처 %d개)", name, time.perf_counter() - t2, Xf.shape[1])
            del yf, Xf, fc
        except (DataError, ValueError) as exc:  # 전망 실패가 일일 모델 결과를 막지 않도록
            log.warning("%s 전망 실패: %s", name, exc)
        done.append(key)
    del raw
    gc.collect()
    return done


def saved_settings(key: str) -> model.Settings:
    run = store.load_run(key)
    return model.Settings.from_dict(run["settings"] if run else None)
