import os
import re
import subprocess
import time
import json
from pathlib import Path
import httpx
import base64
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
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

# --- 사용자 설정 ---
ANIME_ID = 40846
# -------------------

# 전역 변수 설정
WVD_PATH = "./license/device.wvd"
BINARY_DIR = Path("./binaries").resolve()
N_M3U8DL_RE_EXE = BINARY_DIR / "N_m3u8DL-RE.exe"
MKVMERGE_EXE = BINARY_DIR / "mkvmerge.exe"
FFMPEG_EXE = BINARY_DIR / "ffmpeg.exe"
MP4DECRYPT_EXE = BINARY_DIR / "mp4decrypt.exe"

def build_process_env():
    env = os.environ.copy()
    env["PATH"] = str(BINARY_DIR) + os.pathsep + env.get("PATH", "")
    return env

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
        
        lic_response = httpx.post(url=license_url, data=challenge, headers=request_headers)
        lic_response.raise_for_status()

        cdm.parse_license(session_id, lic_response.content)
        keys = []
        for key in cdm.get_keys(session_id):
            if key.type == 'CONTENT':
                keys.append(f"--key {key.kid.hex}:{key.key.hex()}")
        cdm.close(session_id)
        return keys
    except Exception as e:
        print(f"오류: 키 추출 중: {e}")
        return None

def login_and_select_profile_wire():
    # 크롬 사용자 프로필을 재사용하고, 로그인은 수동으로 진행
    options = { 'suppress_connection_errors': True }
    chrome_options = webdriver.ChromeOptions()
    prefs = {"profile.default_content_setting_values.notifications": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--mute-audio")
    profile_dir = (Path("./.chrome-profile")).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    chrome_options.add_argument("--profile-directory=Default")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, seleniumwire_options=options, options=chrome_options)
    
    try:
        driver.get("https://laftel.net/auth/login")
        print("브라우저에서 라프텔 로그인/프로필 선택을 직접 완료해 주세요.")
        input("완료 후 Enter를 누르세요: ")
        print("수동 로그인 단계 완료")
        return driver
    except Exception as e:
        print(f"오류: 로그인/프로필 선택 중: {type(e).__name__}: {e}")
        try:
            print(f"현재 URL: {driver.current_url}")
        except Exception:
            pass
        if 'driver' in locals():
            driver.quit()
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
                        if href and href not in links:
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
                if match not in links:
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

def get_network_data(driver, episode_url):
    # MPD와 라이선스 요청을 순차적으로 기다려 정보 포착
    print(f"\n네트워크 감시 시작: {episode_url}")
    del driver.requests
    driver.get(episode_url)
    try:
        print("MPD 요청 대기 중...")
        driver.wait_for_request(r'\.mpd', timeout=15)
        print("MPD 요청 감지")
        print("라이선스 요청 대기 중...")
        driver.wait_for_request(r'license\.pallycon\.com/ri/licenseManager\.do', timeout=15)
        print("라이선스 요청 감지")
        
        mpd_url, lic_url, lic_headers = None, None, None
        for req in driver.requests:
            if req.response:
                if '.mpd' in req.url and not mpd_url:
                    mpd_url = req.url
                elif 'license.pallycon.com/ri/licenseManager.do' in req.url:
                    lic_url = req.url
                    lic_headers = {k: v for k, v in req.headers.items()}
        return mpd_url, lic_url, lic_headers
    except Exception as e:
        print(f"오류: 네트워크 요청 처리 중: {e}")
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
        print(f"{save_name}.mkv 다운로드 완료")
        
        # 다운로드된 파일 크기 확인
        downloaded_size = expected_filepath.stat().st_size if expected_filepath.exists() else 0
        return True, downloaded_size
    except Exception as e:
        print(f"오류: {episode_num}화 처리 중 예외 발생: {e}")
        return False, 0

if __name__ == "__main__":
    print(ASCII_ART)
    
    DOWNLOAD_DIR_BASE = Path("./downloads")
    if not check_external_tools():
        raise SystemExit(1)
    
    driver = login_and_select_profile_wire()
    if driver:
        episode_links, anime_title = get_episode_links_and_title(driver, ANIME_ID)

        if not anime_title:
            print("오류: 애니메이션 제목을 가져오지 못해 작업을 중단합니다.")
            if 'driver' in locals() and driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            raise SystemExit(1)

        DOWNLOAD_DIR = DOWNLOAD_DIR_BASE / anime_title
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        total_downloaded_bytes = 0
        if episode_links and anime_title:
            failed_episodes = []

            # 1차 시도
            print(f"\n{'='*20} 1차 다운로드를 시작합니다 ({len(episode_links)}개) {'='*20}")
            for i, link in enumerate(episode_links):
                success, size = download_episode(driver, link, i + 1, anime_title, DOWNLOAD_DIR)
                if success:
                    total_downloaded_bytes += size
                else:
                    # 실패한 에피소드의 링크와 번호를 기록
                    failed_episodes.append({'link': link, 'num': i + 1})
            
            # --- 수정된 재시도 로직 ---
            # 1차 시도 후 실패한 에피소드가 있으면 재시도 시작
            retry_pass = 0
            max_retries = 5
            while failed_episodes and retry_pass < max_retries:
                retry_pass += 1
                print(f"\n{'='*20} 실패한 {len(failed_episodes)}개 항목 재시도 ({retry_pass}/{max_retries}) {'='*20}")
                
                # 재시도 시에는 항상 브라우저를 재시작하여 세션 초기화
                driver.quit()
                driver = login_and_select_profile_wire()
                if not driver:
                    print("오류: 재로그인 실패. 재시도를 중단합니다.")
                    break

                # 이번 재시도 회차에서 또 실패한 항목을 기록할 리스트
                failures_this_pass = []
                for episode in failed_episodes:
                    success, size = download_episode(driver, episode['link'], episode['num'], anime_title, DOWNLOAD_DIR)
                    if success:
                        total_downloaded_bytes += size
                    else:
                        failures_this_pass.append(episode)
                
                # 실패 목록을 이번 회차에 실패한 것들로 갱신
                failed_episodes = failures_this_pass
            # --- 재시도 로직 끝 ---

        print("\n모든 작업 완료. 브라우저를 종료합니다")
        
        if total_downloaded_bytes > 0:
            total_gb = total_downloaded_bytes / (1024 ** 3)
            print(f"총 다운로드 용량: {total_gb:.2f} GB")
        else:
            print("다운로드된 파일이 없습니다.")

        if 'driver' in locals() and driver:
            try:
                driver.quit()
            except:
                pass
