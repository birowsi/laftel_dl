# FILE: webui_server.py
# AI_NOTE: FastAPI backend for WebUI. Manages session APIs, download job lifecycle, SSE status/log streaming, and WebUI-only 500MB split-archive jobs for downloaded folders.
import asyncio
import json
import locale
import warnings
import logging
import os
import signal
import shutil
import subprocess
import time
from datetime import datetime
from threading import Lock, Thread
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
import uvicorn

# NOTE: 의존성 경고는 기본적으로 표시하되, 로컬 실행 환경에서 필요하면 환경변수로만 억제한다.
if os.environ.get("LAFTEL_SUPPRESS_PKG_RESOURCES_WARNING", "0") == "1":
    warnings.filterwarnings(
        "ignore",
        message="pkg_resources is deprecated as an API.*",
        category=UserWarning,
    )

import engine
from runtime_support import build_process_env


app = FastAPI(title="laftel web ui backend")
HTML_TEMPLATE_PATH = Path(__file__).with_name("webui_index.html")
NOISY_ACCESS_PATHS = ("/api/status", "/api/logs", "/api/stream")
DOWNLOADS_DIR = Path("./downloads").resolve()
ARCHIVE_DIR = Path("./archives").resolve()
MAX_LOG_LINES = 2000
TRIMMED_LOG_LINES = 1000
DEFAULT_LOG_LIMIT = 200
MAX_LOG_LIMIT = 2000
ARCHIVE_SPLIT_SIZE_MB = 500
_uvicorn_server: Optional[uvicorn.Server] = None


class _AccessPathFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(path in message for path in NOISY_ACCESS_PATHS)


def _configure_access_log_filter():
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessPathFilter) for f in access_logger.filters):
        access_logger.addFilter(_AccessPathFilter())


_configure_access_log_filter()


class DownloadRequest(BaseModel):
    anime_id: int = Field(default=engine.DEFAULT_ANIME_ID, ge=1)
    max_retries: int = Field(default=5, ge=0, le=20)
    episodes: Optional[str] = Field(default=None, max_length=200)


class ArchiveRequest(BaseModel):
    anime_title: str = Field(min_length=1, max_length=200)
    split_size_mb: int = Field(default=ARCHIVE_SPLIT_SIZE_MB, ge=100, le=4096)


class RuntimeState:
    def __init__(self):
        # NOTE: 현재 구조는 로컬 단일 사용자/단일 작업 시나리오를 전제로 한 전역 런타임 상태다.
        self.lock = Lock()
        self.driver = None
        self.running = False
        self.stop_requested = False
        self.last_result = None
        self.last_error = None
        self.worker: Optional[Thread] = None
        self.logs = []
        self.progress = {
            "anime_title": None,
            "total_episodes": 0,
            "processed_episodes": 0,
            "success_episodes": 0,
            "failed_episodes": 0,
            "current_episode": None,
            "last_event": None,
        }
        self.episode_state = {}
        self.archive_running = False
        self.archive_last_result = None
        self.archive_last_error = None
        self.archive_worker: Optional[Thread] = None
        self.change_seq = 0


def _append_log(message: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {message.rstrip()}"
    with state.lock:
        state.logs.append(line)
        if len(state.logs) > MAX_LOG_LINES:
            state.logs = state.logs[-TRIMMED_LOG_LINES:]
        state.change_seq += 1


def _touch_state_locked():
    state.change_seq += 1


def _status_payload_locked():
    return {
        "running": state.running,
        "stop_requested": state.stop_requested,
        "has_session": state.driver is not None,
        "last_result": state.last_result,
        "last_error": state.last_error,
        "progress": dict(state.progress),
        "archive": {
            "running": state.archive_running,
            "last_result": state.archive_last_result,
            "last_error": state.archive_last_error,
        },
    }


class _WebUILogHandler(logging.Handler):
    def emit(self, record):
        _append_log(self.format(record))


state = RuntimeState()


def _list_downloaded_titles():
    if not DOWNLOADS_DIR.exists():
        return []
    return sorted([p.name for p in DOWNLOADS_DIR.iterdir() if p.is_dir()], key=lambda x: x.lower())


def _resolve_download_target(anime_title: str) -> Path:
    name = (anime_title or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="압축 대상 폴더명이 비어 있습니다.")
    target = (DOWNLOADS_DIR / name).resolve()
    if DOWNLOADS_DIR != target and DOWNLOADS_DIR not in target.parents:
        raise HTTPException(status_code=400, detail="유효하지 않은 폴더 경로입니다.")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="다운로드 폴더를 찾지 못했습니다.")
    return target


def _find_7z_executable() -> str | None:
    # 우선순위: 프로젝트 로컬 binaries -> PATH
    for fallback in (
        Path("./binaries/7z.exe").resolve(),
        Path("./binaries/7za.exe").resolve(),
        Path("./binaries/7zr.exe").resolve(),
        Path("./7z.exe").resolve(),
    ):
        if fallback.exists():
            return str(fallback)
    env = build_process_env()
    for candidate in ("7z", "7za", "7zr"):
        found = shutil.which(candidate, path=env.get("PATH"))
        if found:
            return found
    return None


def _cleanup_existing_archive_parts(output_base: Path):
    for item in output_base.parent.glob(f"{output_base.name}*"):
        try:
            if item.is_file():
                item.unlink()
        except Exception:
            pass


def _run_archive_job(target_dir: Path, split_size_mb: int, seven_zip: str):
    source_arg = str(target_dir)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    output_base = ARCHIVE_DIR / f"{target_dir.name}.7z"
    _cleanup_existing_archive_parts(output_base)

    command = [
        seven_zip,
        "a",
        str(output_base),
        source_arg,
        "-t7z",
        "-m0=lzma2",
        "-mx=9",
        "-mfb=273",
        "-ms=on",
        f"-v{split_size_mb}m",
        "-aoa",
        "-bso1",
        "-bse1",
    ]

    _append_log(
        f"분할압축 시작: target={target_dir.name}, split={split_size_mb}MB, output={output_base.name}.001"
    )
    _append_log(f"분할압축 도구: {seven_zip}")

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=locale.getpreferredencoding(False) or "utf-8",
            errors="replace",
            env=build_process_env(),
        )
        last_line = None
        error_context_lines = 0
        if proc.stdout:
            for line in proc.stdout:
                stripped = line.strip()
                if not stripped or stripped == last_line:
                    continue
                last_line = stripped
                lower = stripped.lower()
                if "system error" in lower or "error" in lower:
                    error_context_lines = 3
                if (
                    "error" in lower
                    or "warning" in lower
                    or "creating archive" in lower
                    or "everything is ok" in lower
                    or "%" in stripped
                    or error_context_lines > 0
                ):
                    _append_log(f"[압축] {stripped}")
                if error_context_lines > 0:
                    error_context_lines -= 1
        return_code = proc.wait()
        if return_code != 0:
            raise RuntimeError(f"7z 비정상 종료 (exit={return_code})")

        parts = sorted([p for p in ARCHIVE_DIR.glob(f"{output_base.name}*") if p.is_file()])
        total_bytes = sum(p.stat().st_size for p in parts)
        result = {
            "anime_title": target_dir.name,
            "split_size_mb": split_size_mb,
            "parts": len(parts),
            "total_bytes": total_bytes,
            "output_base": str(output_base),
        }
        with state.lock:
            state.archive_last_result = result
            state.archive_last_error = None
            _touch_state_locked()
        _append_log(
            f"분할압축 완료: {target_dir.name} | parts={len(parts)} | total_bytes={total_bytes}"
        )
    except Exception as e:
        with state.lock:
            state.archive_last_error = f"{type(e).__name__}: {e}"
            _touch_state_locked()
        _append_log(f"분할압축 오류: {type(e).__name__}: {e}")
    finally:
        with state.lock:
            state.archive_running = False
            state.archive_worker = None
            _touch_state_locked()


def _run_download_job(anime_id: int, max_retries: int, episodes: Optional[str] = None):
    web_handler = _WebUILogHandler()
    web_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    engine.LOGGER.addHandler(web_handler)

    def on_job_event(payload: dict):
        event = payload.get("event")

        def recompute(progress_dict):
            values = list(state.episode_state.values())
            success_count = sum(1 for v in values if v == "success")
            failed_count = sum(1 for v in values if v == "failed")
            progress_dict["success_episodes"] = success_count
            progress_dict["failed_episodes"] = failed_count
            progress_dict["processed_episodes"] = success_count + failed_count

        with state.lock:
            progress = state.progress
            progress["last_event"] = event
            if event == "episode_list_ready":
                progress["anime_title"] = payload.get("anime_title")
                progress["total_episodes"] = int(payload.get("count") or 0)
            elif event == "episode_start":
                progress["current_episode"] = payload.get("episode_num")
            elif event == "episode_done":
                episode_num = payload.get("episode_num")
                if episode_num is not None:
                    state.episode_state[int(episode_num)] = "success"
                recompute(progress)
                progress["current_episode"] = None
            elif event == "episode_error":
                episode_num = payload.get("episode_num")
                retriable = bool(payload.get("retriable"))
                if episode_num is not None and not retriable:
                    state.episode_state[int(episode_num)] = "failed"
                recompute(progress)
                progress["current_episode"] = None
            elif event == "episode_skipped":
                episode_num = payload.get("episode_num")
                if episode_num is not None:
                    state.episode_state[int(episode_num)] = "success"
                recompute(progress)
            elif event == "job_done":
                progress["anime_title"] = payload.get("anime_title")
                progress["total_episodes"] = int(payload.get("episode_count") or progress["total_episodes"] or 0)
                failed = int(payload.get("failed_count") or 0)
                total = int(progress["total_episodes"] or 0)
                progress["failed_episodes"] = failed
                progress["success_episodes"] = max(total - failed, 0)
                progress["processed_episodes"] = min(total, progress["success_episodes"] + progress["failed_episodes"])
                progress["current_episode"] = None
            _touch_state_locked()

        if event == "episode_start":
            _append_log(f"회차 시작: {payload.get('episode_num')}화")
        elif event == "episode_done":
            _append_log(f"회차 완료: {payload.get('episode_num')}화")
        elif event == "episode_error":
            _append_log(f"회차 실패: {payload.get('episode_num')}화 ({payload.get('reason')})")

    try:
        _append_log(f"다운로드 작업 시작: anime_id={anime_id}, max_retries={max_retries}, episodes={episodes or 'ALL'}")
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
            on_event=on_job_event,
            episode_selection=episodes,
        )
        with state.lock:
            state.driver = driver
            state.last_result = result
            state.last_error = None
            _touch_state_locked()
        _append_log(
            f"다운로드 요약: title={result.get('anime_title')} | episodes={result.get('episode_count')} | "
            f"failed={result.get('failed_count')} | bytes={result.get('downloaded_bytes')}"
        )
        _append_log("다운로드 작업 종료: 성공")
    except Exception as e:
        with state.lock:
            state.last_error = f"{type(e).__name__}: {e}"
            _touch_state_locked()
        _append_log(f"다운로드 작업 오류: {type(e).__name__}: {e}")
    finally:
        engine.LOGGER.removeHandler(web_handler)
        with state.lock:
            state.running = False
            state.stop_requested = False
            _touch_state_locked()


def _signal_process_shutdown_later(delay_sec: float = 0.3):
    def _worker():
        time.sleep(delay_sec)
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except Exception as e:
            _append_log(f"경고: 프로세스 종료 시그널 전송 실패: {type(e).__name__}: {e}")

    Thread(target=_worker, daemon=True).start()


def _request_graceful_server_shutdown() -> bool:
    global _uvicorn_server
    if _uvicorn_server is None:
        return False
    _uvicorn_server.should_exit = True
    return True


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException):
    if 400 <= int(exc.status_code) < 500:
        _append_log(
            f"요청 오류: {request.method} {request.url.path} -> {exc.status_code} ({exc.detail})"
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def handle_validation_exception(request: Request, exc: RequestValidationError):
    _append_log(f"요청 유효성 오류: {request.method} {request.url.path} -> {exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/api/stream")
async def stream_updates(request: Request, limit: int = DEFAULT_LOG_LIMIT):
    limit = max(1, min(limit, MAX_LOG_LIMIT))

    async def event_generator():
        last_seq = -1
        next_ping_at = time.time() + 15
        while True:
            if await request.is_disconnected():
                break

            payload = None
            with state.lock:
                if state.change_seq != last_seq:
                    last_seq = state.change_seq
                    payload = {
                        "status": _status_payload_locked(),
                        "lines": state.logs[-limit:],
                    }

            if payload is not None:
                yield f"event: snapshot\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                next_ping_at = time.time() + 15
            elif time.time() >= next_ping_at:
                yield "event: ping\ndata: {}\n\n"
                next_ping_at = time.time() + 15

            await asyncio.sleep(0.4)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
            _touch_state_locked()

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
        _touch_state_locked()
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
        _touch_state_locked()
    return {"ok": True, "message": "로그인 완료 및 헤드리스 전환 완료"}


@app.post("/api/download/start")
def start_download(req: DownloadRequest):
    try:
        engine.validate_episode_selection(req.episodes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"회차 입력 형식 오류: {e}") from e

    request_log = (
        f"다운로드 요청 수신: anime_id={req.anime_id}, max_retries={req.max_retries}, "
        f"episodes={req.episodes or 'ALL'}"
    )
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="이미 다운로드가 실행 중입니다.")
        if state.archive_running:
            raise HTTPException(status_code=409, detail="분할압축 실행 중에는 다운로드를 시작할 수 없습니다.")
        if not state.driver:
            raise HTTPException(status_code=400, detail="로그인 세션이 없습니다. 먼저 /api/session/ensure 를 호출하세요.")
        state.running = True
        state.stop_requested = False
        state.last_result = None
        state.last_error = None
        state.logs = []
        state.progress = {
            "anime_title": None,
            "total_episodes": 0,
            "processed_episodes": 0,
            "success_episodes": 0,
            "failed_episodes": 0,
            "current_episode": None,
            "last_event": "job_queued",
        }
        state.episode_state = {}
        _touch_state_locked()
        state.worker = Thread(
            target=_run_download_job,
            args=(req.anime_id, req.max_retries, req.episodes),
            daemon=True,
        )
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
        _touch_state_locked()
    _append_log("중단 요청 수신")
    return {"ok": True, "message": "중단 요청을 전달했습니다. 현재 작업 단위 완료 후 종료됩니다."}


@app.get("/api/status")
def get_status():
    with state.lock:
        return _status_payload_locked()


@app.get("/api/logs")
def get_logs(limit: int = DEFAULT_LOG_LIMIT):
    with state.lock:
        limit = max(1, min(limit, MAX_LOG_LIMIT))
        lines = state.logs[-limit:]
    return {"lines": lines}


@app.get("/api/archive/list")
def list_archive_targets():
    titles = _list_downloaded_titles()
    with state.lock:
        archive_running = state.archive_running
    return {"titles": titles, "archive_running": archive_running}


@app.post("/api/archive/start")
def start_archive(req: ArchiveRequest):
    if req.split_size_mb != ARCHIVE_SPLIT_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"현재 WebUI 분할압축은 {ARCHIVE_SPLIT_SIZE_MB}MB 고정입니다.",
        )

    target_dir = _resolve_download_target(req.anime_title)
    if not any(target_dir.iterdir()):
        raise HTTPException(status_code=400, detail="선택한 폴더가 비어 있습니다.")

    seven_zip = _find_7z_executable()
    if not seven_zip:
        raise HTTPException(status_code=500, detail="7z 실행 파일을 찾지 못했습니다. (7z/7za)")

    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 분할압축을 시작할 수 없습니다.")
        if state.archive_running:
            raise HTTPException(status_code=409, detail="이미 분할압축 작업이 실행 중입니다.")
        state.archive_running = True
        state.archive_last_result = None
        state.archive_last_error = None
        state.archive_worker = Thread(
            target=_run_archive_job,
            args=(target_dir, req.split_size_mb, seven_zip),
            daemon=True,
        )
        _touch_state_locked()
        state.archive_worker.start()

    _append_log(
        f"분할압축 요청 수신: folder={target_dir.name}, split={req.split_size_mb}MB"
    )
    return {
        "ok": True,
        "message": "분할압축 작업을 시작했습니다.",
        "anime_title": target_dir.name,
        "split_size_mb": req.split_size_mb,
    }


@app.post("/api/session/close")
def close_session():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 세션 종료를 할 수 없습니다.")
        driver = state.driver
        state.driver = None
        _touch_state_locked()
    if driver:
        engine.safe_quit_driver(driver)
        _append_log("세션 종료 완료")
    return {"ok": True, "message": "세션 종료 완료"}


@app.post("/api/system/shutdown")
def shutdown_system():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 프로그램 종료를 할 수 없습니다.")
        if state.archive_running:
            raise HTTPException(status_code=409, detail="분할압축 실행 중에는 프로그램 종료를 할 수 없습니다.")
        driver = state.driver
        state.driver = None
        _touch_state_locked()
    if driver:
        engine.safe_quit_driver(driver)
    _append_log("종료 요청 수신: 서버 graceful shutdown을 시작합니다.")
    if not _request_graceful_server_shutdown():
        _append_log("안내: 서버 객체를 찾지 못해 프로세스 SIGINT 종료로 대체합니다.")
        _signal_process_shutdown_later()
    return {"ok": True, "message": "프로그램 종료 요청을 처리했습니다. 잠시 후 서버가 종료됩니다."}


if __name__ == "__main__":
    config = uvicorn.Config(app=app, host="127.0.0.1", port=8000, reload=False)
    _uvicorn_server = uvicorn.Server(config)
    _uvicorn_server.run()
