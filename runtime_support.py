# FILE: runtime_support.py
# AI_NOTE: Shared runtime utilities (logging, process/env helpers, cleanup, tool checks, constants).
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

if os.environ.get("LAFTEL_SUPPRESS_PKG_RESOURCES_WARNING", "0") == "1":
    warnings.filterwarnings(
        "ignore",
        message="pkg_resources is deprecated as an API.*",
        category=UserWarning,
    )

LOGGER = logging.getLogger("laftel")


def setup_logging(level=logging.INFO):
    if LOGGER.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def log_print(*args, **kwargs):
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    msg = sep.join(str(a) for a in args)
    if end and end != "\n":
        msg += end.rstrip("\n")
    LOGGER.info(msg)


setup_logging()


WVD_PATH = "./license/device.wvd"
BINARY_DIR = Path("./binaries").resolve()
N_M3U8DL_RE_EXE = BINARY_DIR / "N_m3u8DL-RE.exe"
MKVMERGE_EXE = BINARY_DIR / "mkvmerge.exe"
MP4DECRYPT_EXE = BINARY_DIR / "mp4decrypt.exe"
LOGIN_WAIT_TIMEOUT_SEC = 300
REQUEST_TIMEOUT_SEC = 60
HTTP_TIMEOUT_SEC = 30
DRIVER_PID_FILE = Path("./.runtime/driver.pid")
DOWNLOAD_MARKER_FILE = Path("./.runtime/inprogress_download.json")


def build_process_env():
    env = os.environ.copy()
    path_entries = [str(BINARY_DIR)]

    # run_webui.bat처럼 venv python.exe를 직접 호출하면 Scripts가 PATH에 안 잡힐 수 있다.
    project_venv_scripts = Path("./.venv/Scripts").resolve()
    if project_venv_scripts.exists():
        path_entries.append(str(project_venv_scripts))

    exe_dir = Path(sys.executable).resolve().parent
    if exe_dir.exists():
        path_entries.append(str(exe_dir))

    existing = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(path_entries + ([existing] if existing else []))
    return env


def _write_driver_pid(pid: int):
    DRIVER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    DRIVER_PID_FILE.write_text(str(pid), encoding="utf-8")


def _read_driver_pid():
    if not DRIVER_PID_FILE.exists():
        return None
    try:
        text = DRIVER_PID_FILE.read_text(encoding="utf-8").strip()
        return int(text)
    except Exception:
        return None


def _clear_driver_pid():
    try:
        if DRIVER_PID_FILE.exists():
            DRIVER_PID_FILE.unlink()
    except Exception:
        pass


def _write_download_marker(payload: dict):
    DOWNLOAD_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_MARKER_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_download_marker():
    if not DOWNLOAD_MARKER_FILE.exists():
        return None
    try:
        return json.loads(DOWNLOAD_MARKER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_download_marker():
    try:
        if DOWNLOAD_MARKER_FILE.exists():
            DOWNLOAD_MARKER_FILE.unlink()
    except Exception:
        pass


def _remove_path(path: Path):
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    return True


def cleanup_stale_download_artifacts():
    marker = _read_download_marker()
    if not marker:
        return

    download_dir_str = marker.get("download_dir")
    save_name = marker.get("save_name")
    if not download_dir_str or not save_name:
        _clear_download_marker()
        return

    download_dir = Path(download_dir_str)
    candidates = [
        download_dir / f"{save_name}.tmp",
        download_dir / f"{save_name}.part",
        download_dir / f"{save_name}.aria2",
        download_dir / f"{save_name}.m4s",
        download_dir / f"{save_name}.mp4",
        download_dir / f"{save_name}.m4a",
        download_dir / f"{save_name}.mkv.tmp",
        download_dir / f"{save_name}.hevc",
        download_dir / f"{save_name}.aac",
    ]

    removed = 0
    for candidate in candidates:
        if _remove_path(candidate):
            removed += 1

    if download_dir.exists():
        for entry in download_dir.glob(f"{save_name}*"):
            name_lower = entry.name.lower()
            if ".tmp" in name_lower or name_lower.endswith(".part") or "temp" in name_lower:
                if _remove_path(entry):
                    removed += 1

    if removed > 0:
        log_print(f"이전 비정상 종료 잔여 파일 정리 완료: {removed}개")
    _clear_download_marker()
    cleanup_stale_root_episode_dirs()


def cleanup_stale_root_episode_dirs():
    root_dir = Path(".").resolve()
    protected_dirs = {
        ".git",
        ".venv",
        ".chrome-profile",
        ".runtime",
        "__pycache__",
        "downloads",
        "archives",
        "license",
        "binaries",
    }
    temp_exts = {
        ".tmp",
        ".part",
        ".aria2",
        ".m4s",
        ".mp4",
        ".m4a",
        ".aac",
        ".hevc",
        ".h265",
        ".h264",
        ".ts",
    }
    removed_dirs = 0
    for entry in root_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in protected_dirs:
            continue
        if not re.search(r"\s\d+화$", entry.name):
            continue

        file_names = []
        for p in entry.rglob("*"):
            if p.is_file():
                file_names.append(p.name.lower())

        if not file_names:
            shutil.rmtree(entry, ignore_errors=True)
            removed_dirs += 1
            continue

        has_final_output = any(name.endswith(".mkv") for name in file_names)
        has_temp_artifact = any(
            name.endswith(ext) for name in file_names for ext in temp_exts
        ) or any(".tmp" in name or "temp" in name for name in file_names)

        if has_final_output:
            continue
        if has_temp_artifact:
            shutil.rmtree(entry, ignore_errors=True)
            removed_dirs += 1

    if removed_dirs > 0:
        log_print(f"루트 임시 회차 폴더 정리 완료: {removed_dirs}개")


def cleanup_stale_driver_process():
    pid = _read_driver_pid()
    if not pid:
        return
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode == 0:
        log_print(f"이전 실행 잔여 프로세스 정리 완료 (PID={pid})")
    _clear_driver_pid()


def cleanup_profile_locked_chrome():
    try:
        script = (
            "$procs=Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*\\.chrome-profile*' }; "
            "foreach($p in $procs){ Stop-Process -Id $p.ProcessId -Force }; "
            "Write-Output ($procs | Measure-Object).Count"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            shell=False,
        )
        if result.returncode == 0:
            killed_count = (result.stdout or "").strip().splitlines()
            killed_text = killed_count[-1] if killed_count else "0"
            try:
                killed = int(killed_text)
            except Exception:
                killed = 0
            if killed > 0:
                log_print(f".chrome-profile 점유 크롬 프로세스 정리 완료: {killed}개")
    except Exception as e:
        log_print(f"경고: 프로필 점유 크롬 정리 중 예외 발생: {type(e).__name__}: {e}")


def safe_quit_driver(driver):
    if driver:
        try:
            driver.quit()
        except Exception as e:
            log_print(f"경고: 브라우저 종료 중 예외 발생: {type(e).__name__}: {e}")
            cleanup_stale_driver_process()
        finally:
            _clear_driver_pid()
            cleanup_profile_locked_chrome()


def check_external_tools():
    required = ["yt-dlp", "N_m3u8DL-RE", "mkvmerge", "mp4decrypt"]
    env = build_process_env()
    log_print(f"외부 도구 점검 PATH 헤드: {env['PATH'].split(os.pathsep)[0]}")
    missing = []
    for tool in required:
        result = subprocess.run(
            ["where", tool],
            capture_output=True,
            text=True,
            env=env,
            shell=False,
        )
        if result.returncode != 0:
            missing.append(tool)
        else:
            log_print(f"확인됨: {tool} -> {result.stdout.strip()}")
    if missing:
        log_print(f"오류: 외부 도구를 찾지 못했습니다: {', '.join(missing)}")
        log_print(f"확인 경로: {BINARY_DIR}")
        return False
    if not N_M3U8DL_RE_EXE.exists():
        log_print(f"오류: {N_M3U8DL_RE_EXE} 파일을 찾지 못했습니다.")
        return False
    if not MKVMERGE_EXE.exists():
        log_print(f"오류: {MKVMERGE_EXE} 파일을 찾지 못했습니다.")
        return False
    if not MP4DECRYPT_EXE.exists():
        log_print(f"오류: {MP4DECRYPT_EXE} 파일을 찾지 못했습니다.")
        log_print("안내: Bento4에서 mp4decrypt.exe를 받아 ./binaries 폴더에 넣어주세요.")
        log_print("안내: https://www.bento4.com/downloads/")
        return False
    return True


def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name)


def normalize_anime_title(name: str) -> str:
    title = (name or "").strip()
    # 라프텔 페이지 제목 꼬리표 제거: "... ㅣ 라프텔", "... | 라프텔"
    title = re.sub(r"\s*[ㅣ|]\s*라프텔\s*$", "", title, flags=re.IGNORECASE)
    return title.strip()
