"""
DRM Extraction & Support Module
===============================

This module provides the necessary cryptographic logic to bypass Widevine DRM.
It heavily relies on the `pywidevine` library and the provided `device.wvd` L3 CDM key.

Key Responsibilities:
1. Parsing the PSSH (Protection System Specific Header) from the MPD manifest.
2. Generating a Widevine license challenge using the local CDM device key.
3. Sending the challenge to the extracted license URL with the captured authentication headers.
4. Parsing the returned license response and extracting the raw decryption keys (KID:KEY pairs).
"""
import base64
import os
import subprocess
import shutil
from pathlib import Path

import httpx
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

from runtime_support import (
    BINARY_DIR,
    HTTP_TIMEOUT_SEC,
    WVD_PATH,
    build_process_env,
    log_print,
)


def find_wv_pssh_offsets(raw: bytes) -> list:
    offsets = []
    offset = 0
    while True:
        offset = raw.find(b"pssh", offset)
        if offset == -1:
            break
        size = int.from_bytes(raw[offset - 4:offset], byteorder="big")
        pssh_offset = offset - 4
        offsets.append(raw[pssh_offset:pssh_offset + size])
        offset += size
    return offsets


def to_pssh(content: bytes) -> list:
    wv_offsets = find_wv_pssh_offsets(content)
    return [base64.b64encode(wv_offset).decode() for wv_offset in wv_offsets]


def get_pssh_from_init(mpd_url, headers):
    log_print("  - init.m4f 파일에서 PSSH 추출 시도")
    init_file = Path("init.m4f")
    if init_file.exists():
        init_file.unlink()
    try:
        env = build_process_env()
        # yt-dlp 단계에서는 깨진 ffmpeg 바이너리(binaries/ffmpeg.exe) 자동 호출을 피한다.
        # 일부 배포본은 avcodec DLL 의존성이 맞지 않아 GUI 오류창을 띄울 수 있다.
        path_items = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        path_items = [p for p in path_items if Path(p).resolve() != BINARY_DIR]
        env["PATH"] = os.pathsep.join(path_items)
        yt_dlp = shutil.which("yt-dlp", path=env.get("PATH", ""))
        if not yt_dlp:
            fallback_candidates = [
                Path("./.venv/Scripts/yt-dlp.exe").resolve(),
                Path("./binaries/yt-dlp.exe").resolve(),
                Path("./yt-dlp.exe").resolve(),
            ]
            for candidate in fallback_candidates:
                if candidate.exists():
                    yt_dlp = str(candidate)
                    break
        if not yt_dlp:
            log_print("  - 오류: yt-dlp 실행 파일을 찾지 못했습니다.")
            return None

        header_args = []
        user_agent = headers.get("user-agent")
        if user_agent:
            header_args.extend(["--user-agent", user_agent])
        command = [
            yt_dlp,
            "--no-warnings",
            "--quiet",
            "--test",
            "--downloader",
            "native",
            "--allow-unplayable-formats",
            "-f",
            "bestvideo[ext=mp4]",
            "-o",
            str(init_file.resolve()),
            mpd_url,
        ] + header_args
        verbose_binary_logs = os.environ.get("LAFTEL_VERBOSE_BINARIES", "0") == "1"
        subprocess.run(
            command,
            check=True,
            capture_output=not verbose_binary_logs,
            env=env,
        )
        if not init_file.exists():
            log_print("  - 오류: init.m4f 파일 다운로드 실패")
            return None
        pssh_list = to_pssh(init_file.read_bytes())
        pssh = None
        for target_pssh in pssh_list:
            if 20 < len(target_pssh) < 220:
                pssh = target_pssh
                break
        if pssh:
            log_print(f"  - PSSH 추출 성공: {pssh[:40]}...")
            return pssh
        log_print("  - 오류: init.m4f에서 PSSH 탐색 실패")
        return None
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, (bytes, bytearray)) else str(e.stderr or "")
        stdout = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, (bytes, bytearray)) else str(e.stdout or "")
        detail = (stderr or stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit={e.returncode}"
        log_print(f"  - 오류: init.m4f 처리 실패 (yt-dlp): {tail}")
        return None
    except Exception as e:
        log_print(f"  - 오류: init.m4f 처리 중: {e}")
        return None
    finally:
        if init_file.exists():
            init_file.unlink()


def get_key_original(pssh, license_url, headers):
    cdm = None
    session_id = None
    try:
        device = Device.load(WVD_PATH)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, PSSH(pssh))

        pallycon_header = headers.get("pallycon-customdata-v2")
        if not pallycon_header:
            raise ValueError("pallycon-customdata-v2 헤더 탐색 실패")

        request_headers = {
            "pallycon-customdata-v2": pallycon_header,
            "Content-Type": "application/octet-stream",
        }

        lic_response = httpx.post(
            url=license_url,
            data=challenge,
            headers=request_headers,
            timeout=HTTP_TIMEOUT_SEC,
        )
        lic_response.raise_for_status()

        cdm.parse_license(session_id, lic_response.content)
        keys = []
        for key in cdm.get_keys(session_id):
            if key.type == "CONTENT":
                keys.append(f"--key {key.kid.hex}:{key.key.hex()}")
        return keys
    except Exception as e:
        log_print(f"오류: 키 추출 중: {e}")
        return None
    finally:
        if cdm is not None and session_id is not None:
            try:
                cdm.close(session_id)
            except Exception:
                pass
