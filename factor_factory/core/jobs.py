"""백그라운드 업데이트 작업 관리 (동시 실행 1개로 제한)."""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from core import pipeline
from core.data import DataError

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = {"running": False, "message": "대기", "error": None, "finished_at": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def start(self, jobs: list, demo: bool) -> bool:
        with self._lock:
            if self._state["running"]:
                return False
            self._state = {"running": True, "message": "시작", "error": None, "finished_at": None}
            self._thread = threading.Thread(target=self._run, args=(jobs, demo),
                                            name="factor-update", daemon=True)
            self._thread.start()
            return True

    def _run(self, jobs: list, demo: bool) -> None:
        try:
            done = pipeline.run_targets(jobs, demo=demo, progress=lambda m: self._set(message=m))
            self._set(message=f"완료 ({len(done)}개 지수)")
        except (DataError, ValueError) as exc:  # 사용자에게 보여줄 수 있는 메시지
            log.warning("업데이트 실패: %s", exc)
            self._set(error=str(exc), message="실패")
        except Exception:  # 내부 정보 노출 방지: 상세는 서버 로그에만
            log.exception("업데이트 중 예외")
            self._set(error="업데이트 중 오류가 발생했습니다. 서버 로그를 확인하세요.", message="실패")
        finally:
            self._set(running=False, finished_at=datetime.now().strftime("%H:%M:%S"))
