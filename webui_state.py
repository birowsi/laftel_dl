# FILE: webui_state.py
# AI_NOTE: WebUI runtime state module. Owns single-user in-memory state, log buffering, session phase tracking, UI-friendly status derivation, and the log handler that bridges engine logs into WebUI logs.
from datetime import datetime
import logging
from threading import Lock, Thread
from typing import Optional


MAX_LOG_LINES = 2000
TRIMMED_LOG_LINES = 1000
DEFAULT_LOG_LIMIT = 200
MAX_LOG_LIMIT = 2000


class RuntimeState:
    def __init__(self):
        # NOTE: 현재 구조는 로컬 단일 사용자/단일 작업 시나리오를 전제로 한 전역 런타임 상태다.
        self.lock = Lock()
        self.driver = None
        self.session_phase = "idle"
        self.session_detail = "먼저 세션을 확보해 주세요."
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
        self.archive_progress_percent = 0
        self.archive_progress_detail = ""
        self.archive_worker: Optional[Thread] = None
        self.change_seq = 0


state = RuntimeState()


def append_log(message: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {message.rstrip()}"
    with state.lock:
        state.logs.append(line)
        if len(state.logs) > MAX_LOG_LINES:
            state.logs = state.logs[-TRIMMED_LOG_LINES:]
        state.change_seq += 1


def touch_state_locked():
    state.change_seq += 1


def set_session_phase_locked(phase: str, detail: str):
    state.session_phase = phase
    state.session_detail = detail
    touch_state_locked()


def derive_ui_state_locked():
    if state.running:
        current_episode = state.progress.get("current_episode")
        processed = int(state.progress.get("processed_episodes") or 0)
        total = int(state.progress.get("total_episodes") or 0)
        if current_episode:
            detail = f"{current_episode}화 처리 중"
        elif total > 0:
            detail = f"{processed}/{total} 진행 중"
        else:
            detail = "작업 준비 중"
        return {"label": "다운로드 진행 중", "detail": detail, "tone": "active"}

    if state.archive_running:
        title = None
        if isinstance(state.archive_last_result, dict):
            title = state.archive_last_result.get("anime_title")
        percent = int(state.archive_progress_percent or 0)
        detail = state.archive_progress_detail or (f"{title} 압축 중" if title else "분할압축 진행 중")
        if percent > 0:
            detail = f"{detail} ({percent}%)"
        return {"label": "분할압축 진행 중", "detail": detail, "tone": "active"}

    if state.last_error:
        return {"label": "오류 발생", "detail": state.last_error, "tone": "danger"}

    if state.session_phase not in ("idle", "ready"):
        session_label_map = {
            "checking_tools": "세션 점검 중",
            "checking_existing_driver": "기존 세션 확인 중",
            "checking_headless_session": "로그인 세션 확인 중",
            "login_required": "로그인 필요",
            "opening_login_window": "로그인 창 준비 중",
            "waiting_for_login": "로그인 대기 중",
            "switching_headless": "백그라운드 세션 전환 중",
        }
        return {
            "label": session_label_map.get(state.session_phase, "세션 처리 중"),
            "detail": state.session_detail,
            "tone": "active" if state.session_phase != "login_required" else "danger",
        }

    if state.driver is not None:
        return {"label": "세션 준비됨", "detail": "작품 ID를 넣고 바로 시작할 수 있습니다.", "tone": "ready"}

    return {"label": "대기 중", "detail": "먼저 세션을 확보해 주세요.", "tone": "idle"}


def status_payload_locked():
    return {
        "running": state.running,
        "stop_requested": state.stop_requested,
        "has_session": state.driver is not None,
        "last_result": state.last_result,
        "last_error": state.last_error,
        "progress": dict(state.progress),
        "ui_state": derive_ui_state_locked(),
        "session": {
            "phase": state.session_phase,
            "detail": state.session_detail,
        },
        "archive": {
            "running": state.archive_running,
            "last_result": state.archive_last_result,
            "last_error": state.archive_last_error,
            "progress_percent": int(state.archive_progress_percent or 0),
            "progress_detail": state.archive_progress_detail,
        },
    }


class WebUILogHandler(logging.Handler):
    def emit(self, record):
        append_log(self.format(record))
