"""
FastAPI WebUI Server Module
===========================

This is the main backend application for the Laftel Downloader Web User Interface.
It provides a REST API and Server-Sent Events (SSE) stream for the frontend.

Key Responsibilities:
1. Serving the main HTML interface (`webui_index.html`) and static assets.
2. Managing the lifecycle of the download job and browser session via API endpoints (`/api/session/*`, `/api/download/*`).
3. Handling multi-anime download parsing and sequential execution.
4. Broadcasting real-time logs and state updates to the frontend via SSE (`/api/stream`).
5. Exposing the split archive feature via API endpoints (`/api/archive/*`).
"""
import asyncio
import json
import warnings
import logging
import os
import signal
import time
import webbrowser
import shutil
from threading import Thread
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse, FileResponse
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
from webui_archive import (
    ARCHIVE_DIR,
    ARCHIVE_SPLIT_SIZE_MB,
    find_7z_executable,
    list_downloaded_titles,
    resolve_download_target,
    run_archive_job,
)
from webui_state import (
    DEFAULT_LOG_LIMIT,
    MAX_LOG_LIMIT,
    WebUILogHandler,
    append_log,
    set_session_phase_locked,
    state,
    status_payload_locked,
    touch_state_locked,
)


app = FastAPI(title="laftel web ui backend")
HTML_TEMPLATE_PATH = Path(__file__).with_name("webui_index.html")
NOISY_ACCESS_PATHS = ("/api/status", "/api/logs", "/api/stream")
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


def _auto_open_webui_browser():
    # 기본 동작: 서버 실행 시 WebUI 탭 자동 오픈.
    # 필요 시 환경변수로 비활성화 가능: LAFTEL_WEBUI_NO_AUTO_OPEN=1
    if os.environ.get("LAFTEL_WEBUI_NO_AUTO_OPEN", "0") == "1":
        return
    url = "http://127.0.0.1:8000"
    Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()


class DownloadRequest(BaseModel):
    anime_id: Optional[int] = Field(default=None, ge=1)
    anime_ids: Optional[str] = Field(default=None, max_length=1000)
    max_retries: int = Field(default=5, ge=0, le=20)
    episodes: Optional[str] = Field(default=None, max_length=200)
    keep_session: bool = True


class ArchiveRequest(BaseModel):
    anime_title: str = Field(min_length=1, max_length=200)
    split_size_mb: int = Field(default=ARCHIVE_SPLIT_SIZE_MB, ge=100, le=4096)


class ArchiveDeleteRequest(BaseModel):
    anime_title: str = Field(min_length=1, max_length=200)


def _dir_size_bytes(path: Path) -> tuple[int, int]:
    total = 0
    file_count = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
                file_count += 1
        except Exception:
            continue
    return total, file_count




def _parse_anime_ids(anime_ids_text: Optional[str], anime_id: Optional[int]) -> list[int]:
    if anime_ids_text and anime_ids_text.strip():
        raw = anime_ids_text.strip()
    elif anime_id is not None:
        raw = str(anime_id)
    else:
        raw = str(engine.DEFAULT_ANIME_ID)

    ids = []
    seen = set()
    for token in [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]:
        if not token.isdigit():
            raise ValueError(f"작품 ID 형식 오류: {token}")
        value = int(token)
        if value <= 0:
            raise ValueError(f"작품 ID는 1 이상이어야 합니다: {token}")
        if value in seen:
            continue
        seen.add(value)
        ids.append(value)
    if not ids:
        raise ValueError("작품 ID가 비어 있습니다.")
    if len(ids) > 30:
        raise ValueError("한 번에 최대 30개 작품 ID만 처리할 수 있습니다.")
    return ids


def _build_episode_plan(anime_ids: list[int], episodes: Optional[str]) -> dict[int, Optional[str]]:
    raw = (episodes or "").strip()
    if not raw:
        return {anime_id: None for anime_id in anime_ids}

    # Normalize common full-width punctuation from IME input.
    normalized = (
        raw.replace("：", ":")
        .replace("；", ";")
        .replace("，", ",")
        .replace("｜", "|")
    )
    raw = normalized

    # Global format: "1-3,5"
    if ":" not in raw:
        engine.validate_episode_selection(raw)
        return {anime_id: raw for anime_id in anime_ids}

    # Per-anime format: "16074:1-3;42947:5,6"
    plan: dict[int, Optional[str]] = {anime_id: None for anime_id in anime_ids}
    seen_ids: set[int] = set()
    allowed_ids = set(anime_ids)
    token_source = raw.replace("\n", ";").replace("|", ";")
    tokens = [token.strip() for token in token_source.split(";") if token.strip()]
    if not tokens:
        raise ValueError("회차 지정 형식이 비어 있습니다.")

    for token in tokens:
        if ":" not in token:
            raise ValueError(f"작품별 회차 형식 오류: '{token}' (예: 16074:1-3)")
        anime_id_text, episode_text = token.split(":", 1)
        anime_id_text = anime_id_text.strip()
        episode_text = episode_text.strip()
        if not anime_id_text.isdigit():
            raise ValueError(f"작품 ID 형식 오류: '{anime_id_text}'")
        target_anime_id = int(anime_id_text)
        if target_anime_id not in allowed_ids:
            raise ValueError(f"작품별 회차 지정에 요청되지 않은 ID가 있습니다: {target_anime_id}")
        if target_anime_id in seen_ids:
            raise ValueError(f"작품별 회차 지정 중복: {target_anime_id}")
        engine.validate_episode_selection(episode_text)
        plan[target_anime_id] = episode_text
        seen_ids.add(target_anime_id)

    return plan


def _run_download_job(
    anime_ids: list[int],
    max_retries: int,
    episode_plan: dict[int, Optional[str]],
    keep_session: bool = True,
):
    web_handler = WebUILogHandler()
    web_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    engine.LOGGER.addHandler(web_handler)
    driver = None

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
                progress["current_episode_stage"] = "prepare"
                progress["current_episode_percent"] = 5
            elif event == "episode_done":
                episode_num = payload.get("episode_num")
                if episode_num is not None:
                    state.episode_state[int(episode_num)] = "success"
                recompute(progress)
                progress["current_episode"] = None
                progress["current_episode_stage"] = "done"
                progress["current_episode_percent"] = 100
            elif event == "episode_error":
                episode_num = payload.get("episode_num")
                retriable = bool(payload.get("retriable"))
                if episode_num is not None and not retriable:
                    state.episode_state[int(episode_num)] = "failed"
                recompute(progress)
                progress["current_episode"] = None
                progress["current_episode_stage"] = "error"
                progress["current_episode_percent"] = 100
            elif event == "episode_skipped":
                episode_num = payload.get("episode_num")
                if episode_num is not None:
                    state.episode_state[int(episode_num)] = "success"
                recompute(progress)
                progress["current_episode_stage"] = "skipped"
                progress["current_episode_percent"] = 100
            elif event == "job_done":
                progress["anime_title"] = payload.get("anime_title")
                progress["total_episodes"] = int(payload.get("episode_count") or progress["total_episodes"] or 0)
                failed = int(payload.get("failed_count") or 0)
                total = int(progress["total_episodes"] or 0)
                progress["failed_episodes"] = failed
                progress["success_episodes"] = max(total - failed, 0)
                progress["processed_episodes"] = min(total, progress["success_episodes"] + progress["failed_episodes"])
                progress["current_episode"] = None
                progress["current_episode_stage"] = "done"
                progress["current_episode_percent"] = 100
            elif event == "episode_stage":
                progress["current_episode"] = payload.get("episode_num") or progress.get("current_episode")
                progress["current_episode_stage"] = payload.get("stage") or progress.get("current_episode_stage")
                progress["current_episode_percent"] = int(payload.get("percent") or progress.get("current_episode_percent") or 0)
            elif event == "retry_pass_start":
                progress["retry_pass"] = int(payload.get("retry_pass") or 0)
                progress["retry_failed_count"] = int(payload.get("failed_count") or 0)
            elif event == "job_stop_requested":
                progress["retry_pass"] = int(payload.get("retry_pass") or progress.get("retry_pass") or 0)
            touch_state_locked()

        if event == "episode_start":
            append_log(f"회차 시작: {payload.get('episode_num')}화")
        elif event == "episode_done":
            append_log(f"회차 완료: {payload.get('episode_num')}화")
        elif event == "episode_error":
            append_log(f"회차 실패: {payload.get('episode_num')}화 ({payload.get('reason')})")
        elif event == "episode_skipped":
            append_log(f"회차 건너뜀: {payload.get('episode_num')}화 (이미 존재)")

    try:
        append_log(
            f"다운로드 작업 시작: anime_ids={','.join(str(v) for v in anime_ids)}, "
            f"max_retries={max_retries}"
        )
        with state.lock:
            driver = state.driver
        if not driver:
            raise RuntimeError("로그인 세션이 없습니다. 먼저 세션을 확보하세요.")
        result = None
        anime_count = len(anime_ids)
        for index, anime_id in enumerate(anime_ids, start=1):
            selected_episodes = episode_plan.get(anime_id)
            if state.stop_requested:
                append_log("중단 요청 감지: 남은 작품 처리를 중단합니다.")
                break
            append_log(
                f"작품 시작 ({index}/{anime_count}): anime_id={anime_id}, "
                f"episodes={selected_episodes or 'ALL'}"
            )
            if engine.has_authenticated_player_access(driver, anime_id=anime_id):
                append_log("다운로드 준비: 기존 백그라운드 세션 재사용")
            else:
                append_log("다운로드 준비: 세션 검증 실패로 백그라운드 세션 재생성 시도")
                runtime_driver = engine.recreate_driver_headless(driver, anime_id=anime_id)
                if not runtime_driver:
                    with state.lock:
                        state.driver = None
                    raise RuntimeError("백그라운드 세션 재생성 또는 세션 검증 실패. 다시 세션을 확보하세요.")
                with state.lock:
                    state.driver = runtime_driver
                driver = runtime_driver

            append_log("다운로드 엔진 실행 시작")
            driver, result = engine.run_download_for_anime(
                driver,
                anime_id,
                max_retries=max_retries,
                should_stop=lambda: state.stop_requested,
                on_event=on_job_event,
                episode_selection=selected_episodes,
            )
            append_log(
                f"작품 종료 ({index}/{anime_count}): anime_id={anime_id} | "
                f"failed={result.get('failed_count') if result else 'n/a'}"
            )

        if result is None:
            result = {
                "anime_id": anime_ids[0] if anime_ids else None,
                "anime_title": None,
                "episode_count": 0,
                "failed_count": 0,
                "downloaded_bytes": 0,
            }

        if keep_session:
            with state.lock:
                state.driver = driver
                state.last_result = result
                state.last_error = None
                set_session_phase_locked("ready", "다운로드가 끝났고 세션을 유지했습니다. 바로 다음 다운로드를 시작할 수 있습니다.")
                touch_state_locked()
        else:
            with state.lock:
                state.driver = None
                state.last_result = result
                state.last_error = None
                set_session_phase_locked("idle", "다운로드가 끝나 세션을 정리했습니다. 다시 시작하려면 세션을 확보해 주세요.")
                touch_state_locked()
            engine.safe_quit_driver(driver)
            driver = None

        append_log(
            f"다운로드 요약: title={result.get('anime_title')} | episodes={result.get('episode_count')} | "
            f"failed={result.get('failed_count')} | bytes={result.get('downloaded_bytes')}"
        )
        append_log(
            f"전체 다운로드 완료: episodes={result.get('episode_count')} / failed={result.get('failed_count')}"
        )
        append_log("다운로드 작업 종료: 성공 (세션 유지)" if keep_session else "다운로드 작업 종료: 성공 (세션 정리)")
    except Exception as e:
        try:
            if driver:
                engine.safe_quit_driver(driver)
        except Exception:
            pass
        with state.lock:
            state.driver = None
            state.last_error = f"{type(e).__name__}: {e}"
            set_session_phase_locked("idle", "오류로 작업이 중단되어 세션을 정리했습니다.")
            touch_state_locked()
        append_log(f"다운로드 작업 오류: {type(e).__name__}: {e}")
    finally:
        engine.LOGGER.removeHandler(web_handler)
        with state.lock:
            state.running = False
            state.stop_requested = False
            touch_state_locked()


def _signal_process_shutdown_later(delay_sec: float = 0.5):
    def _worker():
        time.sleep(delay_sec)
        try:
            os._exit(0)
        except Exception as e:
            append_log(f"경고: 프로세스 강제 종료 실패: {type(e).__name__}: {e}")

    Thread(target=_worker, daemon=True).start()


def _request_graceful_server_shutdown() -> bool:
    global _uvicorn_server
    if _uvicorn_server is None:
        return False
    _uvicorn_server.should_exit = True
    return True


def _on_archive_success(result: dict):
    with state.lock:
        state.archive_last_result = result
        state.archive_last_error = None
        state.archive_progress_percent = 100
        state.archive_progress_detail = "압축 완료"
        touch_state_locked()


def _on_archive_error(message: str):
    with state.lock:
        state.archive_last_error = message
        state.archive_progress_detail = "압축 실패"
        touch_state_locked()


def _on_archive_finished():
    with state.lock:
        state.archive_running = False
        state.archive_worker = None
        touch_state_locked()


def _on_archive_progress(percent: int, detail: str | None = None):
    with state.lock:
        if percent > int(state.archive_progress_percent or 0):
            state.archive_progress_percent = percent
        if detail:
            state.archive_progress_detail = detail[:220]
        touch_state_locked()


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException):
    if 400 <= int(exc.status_code) < 500:
        append_log(
            f"요청 오류: {request.method} {request.url.path} -> {exc.status_code} ({exc.detail})"
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def handle_validation_exception(request: Request, exc: RequestValidationError):
    append_log(f"요청 유효성 오류: {request.method} {request.url.path} -> {exc.errors()}")
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
            if _uvicorn_server is not None and getattr(_uvicorn_server, "should_exit", False):
                break

            payload = None
            with state.lock:
                if state.change_seq != last_seq:
                    last_seq = state.change_seq
                    payload = {
                        "status": status_payload_locked(),
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

@app.get("/background.png")
def get_background_image():
    from fastapi import Response
    import base64
    image_path = Path(__file__).parent / "background.png"
    if image_path.exists():
        return FileResponse(image_path)
    # Transparent 1x1 PNG fallback
    transparent_png_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    return Response(content=base64.b64decode(transparent_png_base64), media_type="image/png")


@app.post("/api/session/ensure")
def ensure_session():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 세션 재설정을 할 수 없습니다.")
        existing_driver = state.driver
        set_session_phase_locked("checking_existing_driver", "기존 세션과 브라우저 상태를 확인하는 중입니다.")

    # 이전 비정상 종료 잔여물 정리 (서버 모드에서는 여기서 수행)
    engine.cleanup_stale_download_artifacts()

    if existing_driver:
        append_log("기존 드라이버 세션 재검증 중...")
        if engine.ensure_logged_in(existing_driver):
            append_log("기존 드라이버 세션 재검증 완료")
            with state.lock:
                set_session_phase_locked("ready", "기존 세션이 유효합니다. 바로 시작할 수 있습니다.")
            return {"ok": True, "message": "기존 드라이버 세션 재검증 완료"}
        append_log("기존 드라이버 세션이 유효하지 않아 종료 후 재확인합니다.")
        engine.safe_quit_driver(existing_driver)
        with state.lock:
            state.driver = None
            set_session_phase_locked("checking_tools", "유효하지 않은 세션을 정리했고, 다시 점검을 이어갑니다.")

    append_log("세션 점검 시작: 외부 도구 확인 중...")
    with state.lock:
        set_session_phase_locked("checking_tools", "외부 도구와 실행 환경을 확인하는 중입니다.")
    if not engine.check_external_tools():
        with state.lock:
            set_session_phase_locked("idle", "외부 도구 점검에 실패했습니다.")
        raise HTTPException(status_code=500, detail="외부 도구 점검 실패")

    append_log("세션 점검: 로그인된 백그라운드 세션 확인 중...")
    with state.lock:
        set_session_phase_locked("checking_headless_session", "저장된 크롬 프로필에서 백그라운드 세션을 찾는 중입니다.")
    driver = engine.get_headless_driver_if_session_exists()
    if not driver:
        append_log("세션 점검 결과: 로그인 필요")
        with state.lock:
            set_session_phase_locked("login_required", "로그인된 세션을 찾지 못했습니다. 로그인 창을 열어 주세요.")
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    append_log("세션 점검 결과: 로그인 세션 확인 완료")
    with state.lock:
        state.driver = driver
        set_session_phase_locked("ready", "로그인 세션 확인이 끝났습니다. 이제 다운로드를 시작할 수 있습니다.")
    return {"ok": True, "message": "기존 로그인 세션 확인 완료"}


@app.post("/api/session/login")
def login_session():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 로그인 세션을 새로 만들 수 없습니다.")
        if state.driver:
            return {"ok": True, "message": "이미 세션이 있습니다."}
        set_session_phase_locked("checking_tools", "로그인 전에 외부 도구 상태를 다시 확인합니다.")

    append_log("로그인 시작: 외부 도구 확인 중...")
    if not engine.check_external_tools():
        with state.lock:
            set_session_phase_locked("idle", "외부 도구 점검에 실패했습니다.")
        raise HTTPException(status_code=500, detail="외부 도구 점검 실패")

    append_log("로그인 창을 여는 중...")
    with state.lock:
        set_session_phase_locked("opening_login_window", "로그인과 프로필 선택을 위한 브라우저 창을 여는 중입니다.")
    visible = engine.create_webdriver_with_profile(headless=False, offscreen=False)
    with state.lock:
        # 로그인 대기 중에도 종료 API가 이 드라이버를 정리할 수 있도록 상태에 등록한다.
        state.driver = visible
        set_session_phase_locked("waiting_for_login", "브라우저에서 로그인과 프로필 선택을 완료해 주세요.")
    if not engine.ensure_logged_in(visible, precheck_session=False):
        append_log("로그인 실패: 세션 확보 실패")
        with state.lock:
            if state.driver is visible:
                state.driver = None
            set_session_phase_locked("idle", "로그인 세션 확보에 실패했습니다.")
        engine.safe_quit_driver(visible)
        raise HTTPException(status_code=500, detail="로그인 세션 확보 실패")

    append_log("백그라운드 세션 전환 중...")
    with state.lock:
        set_session_phase_locked("switching_headless", "로그인된 브라우저 세션을 백그라운드 모드로 전환하는 중입니다.")
    runtime_driver = engine.recreate_driver_headless(visible)
    if not runtime_driver:
        append_log("백그라운드 세션 전환 실패")
        with state.lock:
            if state.driver is visible:
                state.driver = None
            set_session_phase_locked("idle", "백그라운드 세션 전환에 실패했습니다. 다시 로그인해 주세요.")
        raise HTTPException(status_code=500, detail="백그라운드 세션 전환 실패. 다시 로그인 후 시도해 주세요.")

    append_log("로그인 완료 및 백그라운드 세션 전환 완료")
    with state.lock:
        state.driver = runtime_driver
        set_session_phase_locked("ready", "세션 확보가 끝났습니다. 이제 다운로드를 시작할 수 있습니다.")
    return {"ok": True, "message": "로그인 완료 및 백그라운드 세션 전환 완료"}


@app.post("/api/download/start")
def start_download(req: DownloadRequest):
    try:
        anime_ids = _parse_anime_ids(req.anime_ids, req.anime_id)
        episode_plan = _build_episode_plan(anime_ids, req.episodes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if req.episodes and (":" in req.episodes or "：" in req.episodes):
        plan_log = "; ".join(f"{anime_id}:{episode_plan.get(anime_id) or 'ALL'}" for anime_id in anime_ids)
        episode_log = f"PER_ANIME({plan_log})"
    else:
        first_id = anime_ids[0] if anime_ids else None
        episode_log = episode_plan.get(first_id) if first_id is not None else None
        episode_log = episode_log or "ALL"

    request_log = (
        f"다운로드 요청 수신: anime_ids={','.join(str(v) for v in anime_ids)}, max_retries={req.max_retries}, "
        f"episodes={episode_log}, keep_session={req.keep_session}"
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
            "current_episode_stage": "idle",
            "current_episode_percent": 0,
            "last_event": "job_queued",
            "retry_pass": 0,
            "retry_failed_count": 0,
        }
        state.episode_state = {}
        set_session_phase_locked("ready", "다운로드에 사용할 세션이 준비되어 있습니다.")
        state.worker = Thread(
            target=_run_download_job,
            args=(anime_ids, req.max_retries, episode_plan, req.keep_session),
            daemon=True,
        )
        state.worker.start()
    append_log(request_log)
    append_log(
        "다운로드 계획: "
        + " | ".join(f"{anime_id}->{episode_plan.get(anime_id) or 'ALL'}" for anime_id in anime_ids)
    )
    append_log("다운로드 작업 스레드 시작")
    return {"ok": True, "message": "다운로드 작업을 시작했습니다."}


@app.post("/api/download/stop")
def stop_download():
    with state.lock:
        if not state.running:
            return {"ok": True, "message": "실행 중인 다운로드 작업이 없습니다."}
        state.stop_requested = True
        touch_state_locked()
    append_log("중단 요청 수신")
    return {"ok": True, "message": "중단 요청을 전달했습니다. 현재 작업 단위 완료 후 종료됩니다."}


@app.get("/api/status")
def get_status():
    with state.lock:
        return status_payload_locked()


@app.get("/api/logs")
def get_logs(limit: int = DEFAULT_LOG_LIMIT):
    with state.lock:
        limit = max(1, min(limit, MAX_LOG_LIMIT))
        lines = state.logs[-limit:]
    return {"lines": lines}


@app.get("/api/archive/list")
def list_archive_targets():
    titles = list_downloaded_titles()
    with state.lock:
        archive_running = state.archive_running
    return JSONResponse(
        {"titles": titles, "archive_running": archive_running},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.post("/api/archive/start")
def start_archive(req: ArchiveRequest):
    if req.split_size_mb != ARCHIVE_SPLIT_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"현재 WebUI 분할압축은 {ARCHIVE_SPLIT_SIZE_MB}MB 고정입니다.",
        )

    target_dir = resolve_download_target(req.anime_title)
    if not any(target_dir.iterdir()):
        raise HTTPException(status_code=400, detail="선택한 폴더가 비어 있습니다.")

    seven_zip = find_7z_executable()
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
        state.archive_progress_percent = 0
        state.archive_progress_detail = "압축 준비 중"
        state.archive_worker = Thread(
            target=run_archive_job,
            args=(
                target_dir,
                req.split_size_mb,
                seven_zip,
                append_log,
                _on_archive_progress,
                _on_archive_success,
                _on_archive_error,
                _on_archive_finished,
            ),
            daemon=True,
        )
        touch_state_locked()
        state.archive_worker.start()

    append_log(
        f"분할압축 요청 수신: folder={target_dir.name}, split={req.split_size_mb}MB"
    )
    return {
        "ok": True,
        "message": "분할압축 작업을 시작했습니다.",
        "anime_title": target_dir.name,
        "split_size_mb": req.split_size_mb,
    }


@app.get("/api/archive/source-info")
def archive_source_info(anime_title: str):
    target_dir = resolve_download_target(anime_title)
    size_bytes, file_count = _dir_size_bytes(target_dir)
    return {
        "anime_title": target_dir.name,
        "size_bytes": size_bytes,
        "file_count": file_count,
    }


@app.post("/api/archive/delete-source")
def delete_archive_source(req: ArchiveDeleteRequest):
    target_dir = resolve_download_target(req.anime_title)
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 원본 폴더를 삭제할 수 없습니다.")
        if state.archive_running:
            raise HTTPException(status_code=409, detail="분할압축 실행 중에는 원본 폴더를 삭제할 수 없습니다.")
    try:
        shutil.rmtree(target_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"원본 폴더 삭제 실패: {type(e).__name__}: {e}") from e
    append_log(f"원본 폴더 삭제 완료: {target_dir.name}")
    return {"ok": True, "message": "원본 폴더를 삭제했습니다.", "anime_title": target_dir.name}


@app.post("/api/archive/open")
def open_archive_folder():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(ARCHIVE_DIR))
        else:
            import subprocess
            import shutil

            opener = shutil.which("open") or shutil.which("xdg-open")
            if not opener:
                raise RuntimeError("파일 관리자 실행 도구(open/xdg-open)를 찾지 못했습니다.")
            subprocess.Popen([opener, str(ARCHIVE_DIR)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"압축 폴더 열기 실패: {type(e).__name__}: {e}") from e
    append_log(f"압축 폴더 열기: {ARCHIVE_DIR}")
    return {"ok": True, "message": "압축 폴더를 열었습니다.", "path": str(ARCHIVE_DIR)}


@app.post("/api/session/close")
def close_session():
    with state.lock:
        if state.running:
            raise HTTPException(status_code=409, detail="다운로드 실행 중에는 세션 종료를 할 수 없습니다.")
        driver = state.driver
        state.driver = None
        set_session_phase_locked("idle", "세션을 종료했습니다. 다시 사용하려면 세션 확보가 필요합니다.")
    if driver:
        engine.safe_quit_driver(driver)
        append_log("세션 종료 완료")
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
        touch_state_locked()
    if driver:
        engine.safe_quit_driver(driver)
    append_log("종료 요청 수신: 서버 graceful shutdown을 시작합니다.")
    if not _request_graceful_server_shutdown():
        append_log("안내: 서버 객체를 찾지 못해 프로세스 SIGINT 종료로 대체합니다.")
        _signal_process_shutdown_later(0.5)
    else:
        # 안전 장치: graceful 종료가 막히더라도 2초 뒤에 강제 종료
        _signal_process_shutdown_later(2.0)
    return {"ok": True, "message": "프로그램 종료 요청을 처리했습니다. 잠시 후 서버가 종료됩니다."}


if __name__ == "__main__":
    config = uvicorn.Config(app=app, host="127.0.0.1", port=8000, reload=False)
    _uvicorn_server = uvicorn.Server(config)
    _auto_open_webui_browser()
    try:
        _uvicorn_server.run()
    except KeyboardInterrupt:
        pass
    finally:
        with state.lock:
            driver = state.driver
            state.driver = None
            touch_state_locked()
        if driver:
            engine.safe_quit_driver(driver)
