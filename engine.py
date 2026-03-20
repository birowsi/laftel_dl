import os
import re
import subprocess
import time
import warnings
import json
import shutil
import logging
import sys
from pathlib import Path
import httpx
import base64
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

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


setup_logging()


def print(*args, **kwargs):
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    msg = sep.join(str(a) for a in args)
    if end and end != "\n":
        msg += end.rstrip("\n")
    LOGGER.info(msg)

from seleniumwire import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

ASCII_ART = """
██████╗ ██╗   ██╗    
██╔══██╗╚██╗ ██╔╝    
██████╔╝ ╚████╔╝     
██╔══██╗  ╚██╔╝      
██████╔╝   ██║       
╚═════╝    ╚═╝      

██╗  ██╗ █████╗ ███╗   ██╗██████╗ ██╗                            
██║  ██║██╔══██╗████╗  ██║██╔══██╗██║                            
███████║███████║██╔██╗ ██║██████╔╝██║                            
██╔══██║██╔══██║██║╚██╗██║██╔══██╗██║                            
██║  ██║██║  ██║██║ ╚████║██████╔╝██║                            
╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═════╝ ╚═╝ 
"""

# 기본 대상 작품 ID (CLI 기본값)
DEFAULT_ANIME_ID = 40846

# 전역 변수 설정
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
    env["PATH"] = str(BINARY_DIR) + os.pathsep + env.get("PATH", "")
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

    # N_m3u8DL-RE 잔여 임시 폴더/파일 패턴 정리
    if download_dir.exists():
        for entry in download_dir.glob(f"{save_name}*"):
            name_lower = entry.name.lower()
            if ".tmp" in name_lower or name_lower.endswith(".part") or "temp" in name_lower:
                if _remove_path(entry):
                    removed += 1

    if removed > 0:
        print(f"이전 비정상 종료 잔여 파일 정리 완료: {removed}개")
    _clear_download_marker()

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
        print(f"이전 실행 잔여 프로세스 정리 완료 (PID={pid})")
    _clear_driver_pid()

def cleanup_profile_locked_chrome():
    # 자동화 전용 프로필(.chrome-profile)을 점유 중인 크롬 프로세스를 정리
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
                print(f".chrome-profile 점유 크롬 프로세스 정리 완료: {killed}개")
    except Exception as e:
        print(f"경고: 프로필 점유 크롬 정리 중 예외 발생: {type(e).__name__}: {e}")

def safe_quit_driver(driver):
    if driver:
        try:
            driver.quit()
        except Exception as e:
            print(f"경고: 브라우저 종료 중 예외 발생: {type(e).__name__}: {e}")
            cleanup_stale_driver_process()
        finally:
            _clear_driver_pid()
            cleanup_profile_locked_chrome()

def check_external_tools():
    required = ["yt-dlp", "N_m3u8DL-RE", "mkvmerge", "mp4decrypt"]
    env = build_process_env()
    print(f"외부 도구 점검 PATH 헤드: {env['PATH'].split(os.pathsep)[0]}")
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
            print(f"확인됨: {tool} -> {result.stdout.strip()}")
    if missing:
        print(f"오류: 외부 도구를 찾지 못했습니다: {', '.join(missing)}")
        print(f"확인 경로: {BINARY_DIR}")
        return False
    if not N_M3U8DL_RE_EXE.exists():
        print(f"오류: {N_M3U8DL_RE_EXE} 파일을 찾지 못했습니다.")
        return False
    if not MKVMERGE_EXE.exists():
        print(f"오류: {MKVMERGE_EXE} 파일을 찾지 못했습니다.")
        return False
    if not MP4DECRYPT_EXE.exists():
        print(f"오류: {MP4DECRYPT_EXE} 파일을 찾지 못했습니다.")
        print("안내: Bento4에서 mp4decrypt.exe를 받아 ./binaries 폴더에 넣어주세요.")
        print("안내: https://www.bento4.com/downloads/")
        return False
    return True

def sanitize_filename(name):
    # 파일명으로 사용할 수 없는 특수문자 제거
    return re.sub(r'[\\/*?:"<>|]', "", name)


def is_target_player_link(href: str, anime_id: int) -> bool:
    if not href:
        return False
    match = re.search(r"/player/(\d+)/(\d+)", href)
    if not match:
        return False
    return int(match.group(1)) == int(anime_id)

# PSSH 추출 관련 함수
def find_wv_pssh_offsets(raw: bytes) -> list:
    offsets = []
    offset = 0
    while True:
        offset = raw.find(b'pssh', offset)
        if offset == -1:
            break
        size = int.from_bytes(raw[offset-4:offset], byteorder='big')
        pssh_offset = offset - 4
        offsets.append(raw[pssh_offset:pssh_offset+size])
        offset += size
    return offsets

def to_pssh(content: bytes) -> list:
    wv_offsets = find_wv_pssh_offsets(content)
    return [base64.b64encode(wv_offset).decode() for wv_offset in wv_offsets]

def get_pssh_from_init(mpd_url, headers):
    # init.m4f 파일에서 PSSH 추출
    print("  - init.m4f 파일에서 PSSH 추출 시도")
    init_file = Path("init.m4f")
    if init_file.exists():
        init_file.unlink()
    try:
        header_args = []
        user_agent = headers.get('user-agent')
        if user_agent:
            header_args.extend(['--user-agent', user_agent])
        command = [
            'yt-dlp', '--no-warnings', '--quiet', '--test',
            '--allow-unplayable-formats', '-f', 'bestvideo[ext=mp4]',
            '-o', str(init_file.resolve()), mpd_url
        ] + header_args
        subprocess.run(command, check=True, capture_output=True, env=build_process_env())
        if not init_file.exists():
            print("  - 오류: init.m4f 파일 다운로드 실패")
            return None
        pssh_list = to_pssh(init_file.read_bytes())
        pssh = None
        for target_pssh in pssh_list:
            if 20 < len(target_pssh) < 220:
                pssh = target_pssh
                break
        if pssh:
            print(f"  - PSSH 추출 성공: {pssh[:40]}...")
            return pssh
        else:
            print("  - 오류: init.m4f에서 PSSH 탐색 실패")
            return None
    except Exception as e:
        print(f"  - 오류: init.m4f 처리 중: {e}")
        return None
    finally:
        if init_file.exists():
            init_file.unlink()

def get_key_original(pssh, license_url, headers):
    # PSSH와 라이선스 정보로 복호화 키 추출
    cdm = None
    session_id = None
    try:
        device = Device.load(WVD_PATH)
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, PSSH(pssh))
        
        pallycon_header = headers.get('pallycon-customdata-v2')
        if not pallycon_header:
            raise ValueError("pallycon-customdata-v2 헤더 탐색 실패")
        
        request_headers = {
            "pallycon-customdata-v2": pallycon_header,
            "Content-Type": "application/octet-stream"
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
            if key.type == 'CONTENT':
                keys.append(f"--key {key.kid.hex}:{key.key.hex()}")
        return keys
    except Exception as e:
        print(f"오류: 키 추출 중: {e}")
        return None
    finally:
        if cdm is not None and session_id is not None:
            try:
                cdm.close(session_id)
            except Exception:
                pass

def is_login_url(url):
    url = (url or "").lower()
    return "/auth/" in url


def is_home_url(url):
    url = (url or "").lower().rstrip("/")
    return url == "https://laftel.net"

def has_authenticated_player_access(driver, anime_id=DEFAULT_ANIME_ID):
    # 홈 URL 대신 보호 리소스(플레이어 접근 가능 여부)로 세션 검증
    try:
        original_url = driver.current_url
        item_url = f"https://laftel.net/item/{anime_id}"
        driver.get(item_url)
        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/player/']")))
        player_url = None
        for element in driver.find_elements(By.CSS_SELECTOR, "a[href*='/player/']"):
            href = element.get_attribute("href")
            if is_target_player_link(href, anime_id):
                player_url = href
                break
        if not player_url:
            return False

        driver.get(player_url)
        time.sleep(1)
        if is_login_url(driver.current_url):
            return False

        # 플레이어 영역에서 비로그인 전용 안내 문구/버튼이 보이면 비로그인으로 판정
        login_required_markers = driver.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.), '작품 감상을 위해 로그인이 필요해요.')]"
        )
        if login_required_markers:
            return False
        login_buttons = driver.find_elements(
            By.XPATH,
            "//a[contains(normalize-space(.), '로그인') or contains(@href, '/auth/login')]"
            " | //button[contains(normalize-space(.), '로그인')]"
        )
        if login_buttons:
            return False

        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#root-video-fullscreen")))
            return True
        except Exception:
            # 플레이어 루트가 늦게 뜨는 경우를 고려한 완화 판정
            return ("/player/" in (driver.current_url or "")) and (not is_login_url(driver.current_url))
    except Exception:
        return False
    finally:
        # 세션 확인 때문에 플레이어로 이동한 뒤에는 원래 페이지로 복귀
        try:
            if original_url and not is_login_url(original_url):
                driver.get(original_url)
        except Exception:
            pass

def create_webdriver_with_profile(headless=False):
    # 크롬 사용자 프로필을 재사용
    cleanup_stale_driver_process()
    options = { 'suppress_connection_errors': True }
    chrome_options = webdriver.ChromeOptions()
    prefs = {"profile.default_content_setting_values.notifications": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--mute-audio")
    if headless:
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-default-browser-check")
        chrome_options.add_argument("--disable-background-networking")
        chrome_options.add_argument("--disable-component-update")
        chrome_options.add_argument("--disable-features=TranslateUI")
        chrome_options.add_argument("--disable-dev-shm-usage")
    profile_dir = (Path("./.chrome-profile")).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    chrome_options.add_argument("--profile-directory=Default")

    cleanup_profile_locked_chrome()
    service = Service(ChromeDriverManager().install())
    try:
        driver = webdriver.Chrome(service=service, seleniumwire_options=options, options=chrome_options)
    except Exception as e:
        print(f"경고: 크롬 드라이버 초기 실행 실패, 복구 후 재시도합니다: {type(e).__name__}")
        cleanup_stale_driver_process()
        cleanup_profile_locked_chrome()
        time.sleep(1)
        driver = webdriver.Chrome(service=service, seleniumwire_options=options, options=chrome_options)
    service_process = getattr(getattr(driver, "service", None), "process", None)
    service_pid = getattr(service_process, "pid", None)
    if service_pid:
        _write_driver_pid(service_pid)
    return driver

def recreate_driver_headless(existing_driver, anime_id=DEFAULT_ANIME_ID):
    # 로그인은 창 모드에서만 처리하고, 이후에는 headless로 전환
    safe_quit_driver(existing_driver)
    driver = create_webdriver_with_profile(headless=True)
    if not has_authenticated_player_access(driver, anime_id=anime_id):
        safe_quit_driver(driver)
        return None
    return driver


def get_or_login_headless_driver(anime_id=DEFAULT_ANIME_ID):
    # 1) 먼저 headless로 세션 확인
    headless = create_webdriver_with_profile(headless=True)
    if has_authenticated_player_access(headless, anime_id=anime_id):
        return headless
    safe_quit_driver(headless)

    # 2) 세션 없으면 창 모드로 로그인 후 headless 전환
    visible = create_webdriver_with_profile(headless=False)
    if not ensure_logged_in(visible):
        safe_quit_driver(visible)
        return None
    return recreate_driver_headless(visible, anime_id=anime_id)


def get_headless_driver_if_session_exists(anime_id=DEFAULT_ANIME_ID):
    # 세션이 살아있는지 headless로만 검사하고, 없으면 None 반환
    headless = create_webdriver_with_profile(headless=True)
    if has_authenticated_player_access(headless, anime_id=anime_id):
        return headless
    safe_quit_driver(headless)
    return None

def ensure_logged_in(driver):
    # URL이 아닌 보호 리소스 접근 가능 여부로 세션을 판정
    try:
        if has_authenticated_player_access(driver):
            print("기존 로그인 세션을 확인했습니다. 바로 진행합니다.")
            return True

        print("로그인이 필요합니다.")
        print("브라우저에서 라프텔 로그인과 프로필 선택을 완료해 주세요.")
        driver.get("https://laftel.net/auth/login")
        print(f"완료되면 자동 감지합니다. (최대 {LOGIN_WAIT_TIMEOUT_SEC}초 대기)")

        deadline = time.time() + LOGIN_WAIT_TIMEOUT_SEC
        prompted_retry = False
        while time.time() < deadline:
            current_url = driver.current_url or ""
            # auth 플로우(이메일 입력/비밀번호 입력/콜백) 중에는 사용자 입력을 방해하지 않는다.
            if is_login_url(current_url):
                time.sleep(1)
                continue

            # 조기 오탐 방지를 위해 메인 페이지로 돌아온 경우에만 완료 검증을 시도한다.
            if not is_home_url(current_url):
                time.sleep(1)
                continue

            if has_authenticated_player_access(driver):
                print("로그인 완료를 감지했습니다.")
                return True

            if not prompted_retry:
                print("로그인 상태를 아직 확인하지 못했습니다. 로그인/프로필 선택을 다시 확인해 주세요.")
                prompted_retry = True
            driver.get("https://laftel.net/auth/login")
            time.sleep(1)

        raise RuntimeError(f"로그인 대기 시간 초과 ({LOGIN_WAIT_TIMEOUT_SEC}초)")
    except Exception as e:
        print(f"오류: 로그인/프로필 선택 중: {type(e).__name__}: {e}")
        try:
            print(f"현재 URL: {driver.current_url}")
        except Exception:
            pass
        return False

def login_and_select_profile_wire():
    driver = create_webdriver_with_profile()
    
    if ensure_logged_in(driver):
        return driver
    else:
        safe_quit_driver(driver)
        return None

def get_episode_links_and_title(driver, anime_id):
    # 재생 페이지 목록에서 링크와 함께 제목도 추출
    try:
        def collect_player_links_with_wait(selectors, timeout=20, min_count=1):
            deadline = time.time() + timeout
            found = []
            while time.time() < deadline:
                links = []
                for selector in selectors:
                    for element in driver.find_elements(By.CSS_SELECTOR, selector):
                        href = element.get_attribute("href")
                        if is_target_player_link(href, anime_id) and href not in links:
                            links.append(href)
                if len(links) >= min_count:
                    return links
                found = links
                time.sleep(0.5)
            return found

        item_page_url = f"https://laftel.net/item/{anime_id}"
        print(f"애니메이션 정보 페이지로 이동: {item_page_url}")
        driver.get(item_page_url)
        wait = WebDriverWait(driver, 20)

        title_candidates = [
            (By.CSS_SELECTOR, "meta[property='og:title']"),
            (By.CSS_SELECTOR, "h1"),
            (By.CSS_SELECTOR, ".sc-b12ebb9a-1"),
        ]
        anime_title = None
        for by, selector in title_candidates:
            try:
                if by == By.CSS_SELECTOR and selector.startswith("meta"):
                    element = wait.until(EC.presence_of_element_located((by, selector)))
                    content = (element.get_attribute("content") or "").strip()
                    if content:
                        anime_title = content
                        break
                else:
                    element = wait.until(EC.presence_of_element_located((by, selector)))
                    text = (element.text or "").strip()
                    if text:
                        anime_title = text
                        break
            except Exception:
                continue
        if not anime_title:
            raise RuntimeError("제목 요소를 찾지 못했습니다.")

        sanitized_title = sanitize_filename(anime_title)
        print(f"애니메이션 제목 '{sanitized_title}' 확인")

        # 1) item 페이지에서 먼저 에피소드 링크를 수집 (가장 안정적)
        links = collect_player_links_with_wait(
            selectors=["#item-tab-view a[href*='/player/']", "a[href*='/player/']"],
            timeout=20,
            min_count=2,
        )

        # item 페이지에서 충분히 수집되면 플레이어 이동 없이 반환
        if len(links) > 1:
            print("item 페이지에서 에피소드 목록 로드 확인")
            print(f"총 {len(links)}개의 에피소드 링크 확보")
            return links, sanitized_title

        # 2) fallback: 첫 에피소드로 이동 후 플레이어 사이드바에서 수집
        first_episode_element = None
        episode_link_candidates = [
            "#item-tab-view ul li a[href*='/player/']",
            "a[href*='/player/']",
        ]
        for selector in episode_link_candidates:
            try:
                first_episode_element = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                if first_episode_element:
                    break
            except Exception:
                continue
        if not first_episode_element:
            raise RuntimeError("첫 에피소드 링크를 찾지 못했습니다.")

        player_page_url = first_episode_element.get_attribute("href")
        print("전체 에피소드 목록 확인을 위해 재생 페이지로 이동")
        driver.get(player_page_url)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#root-video-fullscreen")))
        print("비디오 플레이어 로드 확인")

        links = collect_player_links_with_wait(
            selectors=["aside a[href*='/player/']", "a[href*='/player/']"],
            timeout=25,
            min_count=2,
        )

        # 3) 마지막 fallback: 페이지 소스에서 /player/ 링크 정규식 추출
        if not links:
            for match in re.findall(r'https://laftel\.net/player/\d+/\d+', driver.page_source):
                if is_target_player_link(match, anime_id) and match not in links:
                    links.append(match)

        print("에피소드 목록 로드 확인")
        print(f"총 {len(links)}개의 에피소드 링크 확보")
        return links, sanitized_title
    except Exception as e:
        print(f"오류: 에피소드 링크/제목 추출 중: {type(e).__name__}: {e}")
        try:
            print(f"현재 URL: {driver.current_url}")
        except Exception:
            pass
        return [], None

def get_network_data(driver, episode_url, retries=2):
    # MPD와 라이선스 요청을 순차적으로 기다려 정보 포착
    print(f"\n네트워크 감시 시작: {episode_url}")

    def collect_urls_from_requests():
        mpd_url, lic_url, lic_headers = None, None, None
        for req in driver.requests:
            if not req.response:
                continue
            if '.mpd' in req.url and not mpd_url:
                mpd_url = req.url
            elif 'license.pallycon.com/ri/licenseManager.do' in req.url:
                lic_url = req.url
                lic_headers = {k: v for k, v in req.headers.items()}
        return mpd_url, lic_url, lic_headers

    def trigger_player_activity():
        # 일부 회차는 수동 재생 트리거가 있어야 license 요청이 발생함
        try:
            driver.execute_script(
                """
                const video = document.querySelector('video');
                if (video) {
                    video.muted = true;
                    const p = video.play();
                    if (p && p.catch) p.catch(() => {});
                }
                const playBtn = Array.from(document.querySelectorAll('button,a')).find((el) => {
                    const text = (el.textContent || '').trim();
                    const aria = (el.getAttribute('aria-label') || '').trim();
                    return /재생|play/i.test(text + ' ' + aria);
                });
                if (playBtn) playBtn.click();
                """
            )
        except Exception:
            pass

    attempt = 0
    while attempt <= retries:
        try:
            print(f"네트워크 감시 시도 {attempt + 1}/{retries + 1}")
            del driver.requests
            driver.get(episode_url)
            time.sleep(1)

            print("MPD 요청 대기 중...")
            driver.wait_for_request(r'\.mpd', timeout=REQUEST_TIMEOUT_SEC)
            print("MPD 요청 감지")

            mpd_url, lic_url, lic_headers = collect_urls_from_requests()
            if lic_url and lic_headers:
                print("라이선스 요청 감지")
                return mpd_url, lic_url, lic_headers

            print("라이선스 요청 대기 중...")
            print("재생 트리거 시도 (video.play / 재생 버튼 클릭)")
            trigger_player_activity()
            driver.wait_for_request(r'license\.pallycon\.com/ri/licenseManager\.do', timeout=REQUEST_TIMEOUT_SEC)
            print("라이선스 요청 감지")

            mpd_url, lic_url, lic_headers = collect_urls_from_requests()
            return mpd_url, lic_url, lic_headers
        except Exception as e:
            attempt += 1
            print(f"오류: 네트워크 요청 처리 중: {e}")
            if attempt <= retries:
                print(f"네트워크 요청 재시도 ({attempt}/{retries})")
                time.sleep(1)
            else:
                return None, None, None

def download_episode(driver, link, episode_num, anime_title, download_dir):
    # 한 에피소드의 다운로드를 처리
    save_name = f"{anime_title} {episode_num}화"
    expected_filepath = download_dir / f"{save_name}.mkv"
    if expected_filepath.exists():
        print(f"\n이미 파일이 존재하여 건너뜁니다: {expected_filepath.name}")
        return True, 0 # 성공, 0바이트 다운로드

    print(f"\n{'='*20} {episode_num}화 처리 시작 {'='*20}")
    
    driver.get('about:blank')
    time.sleep(1)

    mpd_url, lic_url, lic_headers = get_network_data(driver, link)

    if not all([mpd_url, lic_url, lic_headers]):
        print(f"오류: {episode_num}화 네트워크 정보 확보 실패")
        return False, 0 # 실패
    
    try:
        env = build_process_env()
        print(f"실행 PATH 헤드: {env['PATH'].split(os.pathsep)[0]}")
        pssh = get_pssh_from_init(mpd_url, lic_headers)
        if not pssh:
            print(f"오류: {episode_num}화 PSSH 확보 실패")
            return False, 0
        
        keys = get_key_original(pssh, lic_url, lic_headers)
        if not keys:
            print(f"오류: {episode_num}화 키 추출 실패")
            return False, 0
        
        print(f"키 추출 성공: {' '.join(keys)}")

        key_args = [item for sublist in [k.split() for k in keys] for item in sublist]
        _write_download_marker(
            {
                "save_name": save_name,
                "download_dir": str(download_dir.resolve()),
                "episode_num": episode_num,
                "updated_at": int(time.time()),
            }
        )
        command = [
            str(N_M3U8DL_RE_EXE), mpd_url,
            '--save-name', save_name,
            '--save-dir', str(download_dir.resolve()),
            '-M', 'format=mkv:muxer=mkvmerge',
            '--select-video', 'best',
            '--select-audio', 'best',
            '--no-log'
        ] + key_args
        
        print(f"다운로드 시작: {save_name}.mkv")
        subprocess.run(command, check=True, env=env)
        _clear_download_marker()
        print(f"{save_name}.mkv 다운로드 완료")
        
        # 다운로드된 파일 크기 확인
        downloaded_size = expected_filepath.stat().st_size if expected_filepath.exists() else 0
        return True, downloaded_size
    except Exception as e:
        print(f"오류: {episode_num}화 처리 중 예외 발생: {e}")
        return False, 0

def run_download_for_anime(driver, anime_id, max_retries=5, should_stop=None):
    download_dir_base = Path("./downloads")
    episode_links, anime_title = get_episode_links_and_title(driver, anime_id)

    if not anime_title:
        raise RuntimeError("애니메이션 제목을 가져오지 못해 작업을 중단합니다.")

    download_dir = download_dir_base / anime_title
    download_dir.mkdir(parents=True, exist_ok=True)

    total_downloaded_bytes = 0
    failed_episodes = []

    if episode_links:
        print(f"\n{'='*20} 1차 다운로드를 시작합니다 ({len(episode_links)}개) {'='*20}")
        for i, link in enumerate(episode_links):
            if should_stop and should_stop():
                print("중단 요청 감지: 현재 작업을 종료합니다.")
                break
            success, size = download_episode(driver, link, i + 1, anime_title, download_dir)
            if success:
                total_downloaded_bytes += size
            else:
                failed_episodes.append({'link': link, 'num': i + 1})
    else:
        print("다운로드 가능한 에피소드 링크를 찾지 못했습니다.")

    retry_pass = 0
    while failed_episodes and retry_pass < max_retries:
        if should_stop and should_stop():
            print("중단 요청 감지: 재시도를 시작하지 않습니다.")
            break
        retry_pass += 1
        print(f"\n{'='*20} 실패한 {len(failed_episodes)}개 항목 재시도 ({retry_pass}/{max_retries}) {'='*20}")

        safe_quit_driver(driver)
        driver = login_and_select_profile_wire()
        if not driver:
            print("오류: 재로그인 실패. 재시도를 중단합니다.")
            break

        failures_this_pass = []
        for episode in failed_episodes:
            if should_stop and should_stop():
                print("중단 요청 감지: 재시도 루프를 종료합니다.")
                break
            success, size = download_episode(driver, episode['link'], episode['num'], anime_title, download_dir)
            if success:
                total_downloaded_bytes += size
            else:
                failures_this_pass.append(episode)
        failed_episodes = failures_this_pass

    return driver, {
        "anime_id": anime_id,
        "anime_title": anime_title,
        "episode_count": len(episode_links),
        "failed_count": len(failed_episodes),
        "downloaded_bytes": total_downloaded_bytes,
    }
