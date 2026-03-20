import warnings
import logging
import os
import time
from datetime import datetime
from threading import Lock, Thread
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

warnings.filterwarnings(
    "ignore",
    message="pkg_resources is deprecated as an API.*",
    category=UserWarning,
)

import engine


app = FastAPI(title="laftel web ui backend")
HTML_TEMPLATE_PATH = Path(__file__).with_name("webui_index.html")


class DownloadRequest(BaseModel):
    anime_id: int = Field(default=engine.DEFAULT_ANIME_ID, ge=1)
    max_retries: int = Field(default=5, ge=0, le=20)


class RuntimeState:
    def __init__(self):
        self.lock = Lock()
        self.driver = None
        self.running = False
        self.stop_requested = False
        self.last_result = None
        self.last_error = None
        self.worker: Optional[Thread] = None
        self.logs = []


def _append_log(message: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {message.rstrip()}"
    with state.lock:
        state.logs.append(line)
        if len(state.logs) > 2000:
            state.logs = state.logs[-1000:]


class _WebUILogHandler(logging.Handler):
    def emit(self, record):
        _append_log(self.format(record))


state = RuntimeState()


def _run_download_job(anime_id: int, max_retries: int):
    web_handler = _WebUILogHandler()
    web_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    engine.LOGGER.addHandler(web_handler)
    try:
        _append_log(f"다운로드 작업 시작: anime_id={anime_id}, max_retries={max_retries}")
        with state.lock:
            driver = state.driver
        if not driver:
            raise RuntimeError("로그인 세션이 없습니다. 먼저 세션을 확보하세요.")

        _append_log("다운로드 준비: 헤드리스 전환 시도")
        headless_driver = engine.recreate_driver_headless(driver, anime_id=anime_id)
        if not headless_driver:
            with state.lock:
                state.driver = None
            raise RuntimeError("헤드리스 전환 또는 세션 검증 실패. 다시 세션을 확보하세요.")
        with state.lock:
            state.driver = headless_driver
        driver = headless_driver

        _append_log("다운로드 엔진 실행 시작")
        driver, result = engine.run_download_for_anime(
            driver,
            anime_id,
            max_retries=max_retries,
            should_stop=lambda: state.stop_requested,
        )
        with state.lock:
            state.driver = driver
            state.last_result = result
            state.last_error = None
        _append_log(
            f"다운로드 요약: title={result.get('anime_title')} | episodes={result.get('episode_count')} | "
            f"failed={result.get('failed_count')} | bytes={result.get('downloaded_bytes')}"
        )
        _append_log("다운로드 작업 종료: 성공")
    except Exception as e:
        with state.lock:
            state.last_error = f"{type(e).__name__}: {e}"
        _append_log(f"다운로드 작업 오류: {type(e).__name__}: {e}")
    finally:
        engine.LOGGER.removeHandler(web_handler)
        with state.lock:
            state.running = False
            state.stop_requested = False


def _shutdown_process_later(delay_sec: float = 0.5):
    def _worker():
        time.sleep(delay_sec)
        os._exit(0)
    Thread(target=_worker, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    html = HTML_TEMPLATE_PATH.read_text(encoding="utf-8")
    html = html.replace("__ASCII_ART__", engine.ASCII_ART.strip("\n"))
    html = html.replace("__DEFAULT_ANIME_ID__", str(engine.DEFAULT_ANIME_ID))
    return html


@app.post("/api/session/ensure")
def ensure_session():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 세션 재설정을 할 수 없습니다.")
        existing_driver = state.driver

    # 이전 비정상 종료 잔여물 정리 (서버 모드에서는 여기서 수행)
    engine.cleanup_stale_download_artifacts()

    if existing_driver:
        _append_log("기존 드라이버 세션 재검증 중...")
        if engine.ensure_logged_in(existing_driver):
            _append_log("기존 드라이버 세션 재검증 완료")
            return {"ok": True, "message": "기존 드라이버 세션 재검증 완료"}
        _append_log("기존 드라이버 세션이 유효하지 않아 종료 후 재확인합니다.")
        engine.safe_quit_driver(existing_driver)
        with state.lock:
            state.driver = None

    _append_log("세션 점검 시작: 외부 도구 확인 중...")
    if not engine.check_external_tools():
        raise HTTPException(status_code=500, detail="외부 도구 점검 실패")

    _append_log("세션 점검: 로그인된 세션(headless) 확인 중...")
    driver = engine.get_headless_driver_if_session_exists()
    if not driver:
        _append_log("세션 점검 결과: 로그인 필요")
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    _append_log("세션 점검 결과: 로그인 세션 확인 완료")
    with state.lock:
        state.driver = driver
    return {"ok": True, "message": "기존 로그인 세션 확인 완료"}


@app.post("/api/session/login")
def login_session():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 로그인 세션을 새로 만들 수 없습니다.")
        if state.driver:
            return {"ok": True, "message": "이미 세션이 있습니다."}

    _append_log("로그인 시작: 외부 도구 확인 중...")
    if not engine.check_external_tools():
        raise HTTPException(status_code=500, detail="외부 도구 점검 실패")

    _append_log("로그인 창을 여는 중...")
    visible = engine.login_and_select_profile_wire()
    if not visible:
        _append_log("로그인 실패: 세션 확보 실패")
        raise HTTPException(status_code=500, detail="로그인 세션 확보 실패")

    _append_log("헤드리스 전환 중...")
    headless = engine.recreate_driver_headless(visible)
    if not headless:
        _append_log("헤드리스 전환 실패")
        raise HTTPException(status_code=500, detail="헤드리스 전환 실패. 다시 로그인 후 시도해 주세요.")

    _append_log("로그인 완료 및 헤드리스 전환 완료")
    with state.lock:
        state.driver = headless
    return {"ok": True, "message": "로그인 완료 및 헤드리스 전환 완료"}


@app.post("/api/download/start")
def start_download(req: DownloadRequest):
    request_log = f"다운로드 요청 수신: anime_id={req.anime_id}, max_retries={req.max_retries}"
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="이미 다운로드가 실행 중입니다.")
        if not state.driver:
            raise HTTPException(status_code=400, detail="로그인 세션이 없습니다. 먼저 /api/session/ensure 를 호출하세요.")
        state.running = True
        state.stop_requested = False
        state.last_result = None
        state.last_error = None
        state.logs = []
        state.worker = Thread(target=_run_download_job, args=(req.anime_id, req.max_retries), daemon=True)
        state.worker.start()
    _append_log(request_log)
    _append_log("다운로드 작업 스레드 시작")
    return {"ok": True, "message": "다운로드 작업을 시작했습니다."}


@app.post("/api/download/stop")
def stop_download():
    with state.lock:
        if not state.running:
            return {"ok": True, "message": "실행 중인 다운로드 작업이 없습니다."}
        state.stop_requested = True
    _append_log("중단 요청 수신")
    return {"ok": True, "message": "중단 요청을 전달했습니다. 현재 작업 단위 완료 후 종료됩니다."}


@app.get("/api/status")
def get_status():
    with state.lock:
        return {
            "running": state.running,
            "stop_requested": state.stop_requested,
            "has_session": state.driver is not None,
            "last_result": state.last_result,
            "last_error": state.last_error,
        }


@app.get("/api/logs")
def get_logs(limit: int = 200):
    with state.lock:
        limit = max(1, min(limit, 2000))
        lines = state.logs[-limit:]
    return {"lines": lines}


@app.post("/api/session/close")
def close_session():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 세션 종료를 할 수 없습니다.")
        driver = state.driver
        state.driver = None
    if driver:
        engine.safe_quit_driver(driver)
        _append_log("세션 종료 완료")
    return {"ok": True, "message": "세션 종료 완료"}


@app.post("/api/system/shutdown")
def shutdown_system():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 프로그램 종료를 할 수 없습니다.")
        driver = state.driver
        state.driver = None
    if driver:
        engine.safe_quit_driver(driver)
    _append_log("종료 요청 수신: 서버를 종료합니다.")
    _shutdown_process_later()
    return {"ok": True, "message": "프로그램 종료 요청을 처리했습니다. 잠시 후 서버가 종료됩니다."}


if __name__ == "__main__":
    uvicorn.run("webui_server:app", host="127.0.0.1", port=8000, reload=False)
