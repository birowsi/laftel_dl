# FILE: webui_archive.py
# AI_NOTE: WebUI archive module. Finds 7z executables, validates download targets, runs 500MB split archive jobs, and reports progress/errors through injected callbacks.
import locale
import shutil
import subprocess
import re
from pathlib import Path

from fastapi import HTTPException

from runtime_support import build_process_env


DOWNLOADS_DIR = Path("./downloads").resolve()
ARCHIVE_DIR = Path("./archives").resolve()
ARCHIVE_SPLIT_SIZE_MB = 500


def _normalize_progress_detail(raw_line: str) -> str:
    text = (raw_line or "").strip()
    # 7z 진행 라인 예: "80% 2 + C:\\...\\2화.mkv"
    text = re.sub(r"^\s*\d{1,3}%\s*", "", text)
    text = re.sub(r"^\d+\s*\+\s*", "", text)
    text = text.replace("\\", "/").strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text or "압축 중"


def list_downloaded_titles():
    if not DOWNLOADS_DIR.exists():
        return []
    return sorted([p.name for p in DOWNLOADS_DIR.iterdir() if p.is_dir()], key=lambda x: x.lower())


def resolve_download_target(anime_title: str) -> Path:
    name = (anime_title or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="압축 대상 폴더명이 비어 있습니다.")
    target = (DOWNLOADS_DIR / name).resolve()
    if DOWNLOADS_DIR != target and DOWNLOADS_DIR not in target.parents:
        raise HTTPException(status_code=400, detail="유효하지 않은 폴더 경로입니다.")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="다운로드 폴더를 찾지 못했습니다.")
    return target


def find_7z_executable() -> str | None:
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


def cleanup_existing_archive_parts(output_base: Path):
    for item in output_base.parent.glob(f"{output_base.name}*"):
        try:
            if item.is_file():
                item.unlink()
        except Exception:
            pass


def run_archive_job(
    target_dir: Path,
    split_size_mb: int,
    seven_zip: str,
    append_log,
    on_progress,
    on_success,
    on_error,
    on_finished,
):
    source_arg = str(target_dir)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    output_base = ARCHIVE_DIR / f"{target_dir.name}.7z"
    cleanup_existing_archive_parts(output_base)

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
        "-bsp1",
        "-bso1",
        "-bse1",
    ]

    append_log(
        f"분할압축 시작: target={target_dir.name}, split={split_size_mb}MB, output={output_base.name}.001"
    )
    append_log(f"분할압축 도구: {seven_zip}")

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
        last_percent = -1
        error_context_lines = 0
        if proc.stdout:
            buf = ""
            while True:
                ch = proc.stdout.read(1)
                if ch == "" and proc.poll() is not None:
                    if buf.strip():
                        stripped = buf.strip()
                        if stripped and stripped != last_line:
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
                                append_log(f"[압축] {stripped}")
                            for match in re.finditer(r"(\d{1,3})%", stripped):
                                try:
                                    percent = max(0, min(int(match.group(1)), 100))
                                except Exception:
                                    continue
                                if percent > last_percent:
                                    last_percent = percent
                                    on_progress(percent, _normalize_progress_detail(stripped))
                    break

                if not ch:
                    continue

                if ch in ("\r", "\n"):
                    stripped = buf.strip()
                    buf = ""
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
                        append_log(f"[압축] {stripped}")
                    for match in re.finditer(r"(\d{1,3})%", stripped):
                        try:
                            percent = max(0, min(int(match.group(1)), 100))
                        except Exception:
                            continue
                        if percent > last_percent:
                            last_percent = percent
                            on_progress(percent, _normalize_progress_detail(stripped))
                    if error_context_lines > 0:
                        error_context_lines -= 1
                else:
                    buf += ch
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
        on_success(result)
        append_log(
            f"분할압축 완료: {target_dir.name} | parts={len(parts)} | total_bytes={total_bytes}"
        )
    except Exception as e:
        on_error(f"{type(e).__name__}: {e}")
        append_log(f"분할압축 오류: {type(e).__name__}: {e}")
    finally:
        on_finished()
