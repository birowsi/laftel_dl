import os
import re
import subprocess
import time
import json
from pathlib import Path
from dotenv import load_dotenv
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
        subprocess.run(command, check=True, capture_output=True)
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
    # selenium-wire 드라이버로 로그인 및 프로필 선택
    load_dotenv()
    LAFTEL_EMAIL = os.getenv("LAFTEL_EMAIL")
    LAFTEL_PASSWORD = os.getenv("LAFTEL_PASSWORD")
    
    options = { 'suppress_connection_errors': True }
    chrome_options = webdriver.ChromeOptions()
    prefs = {"profile.default_content_setting_values.notifications": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--mute-audio")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, seleniumwire_options=options, options=chrome_options)
    
    wait = WebDriverWait(driver, 10)
    try:
        driver.get("https://laftel.net/auth/login")
        start_email_selector = "#root > div.sc-4f246076-1.kxlezL > div > div > div.sc-755b8e85-5.jFLdwS > div > div.sc-c41aa92c-3.jElMpk > div > button"
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, start_email_selector))).click()
        email_input_selector = "#root > div.sc-4f246076-1.kxlezL > div > div > form > div > div.sc-e3dba43f-0.efOPnx > div.sc-e3dba43f-3.gcfHtI > input"
        wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, email_input_selector))).send_keys(LAFTEL_EMAIL)
        driver.find_element(By.CSS_SELECTOR, "#root > div.sc-4f246076-1.kxlezL > div > div > form > button").click()
        password_input_selector = "#root > div.sc-4f246076-1.kxlezL > div > div > form > div > div.sc-7eaf7183-4.iyVQgC > div > div.sc-e3dba43f-3.gcfHtI > input"
        wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, password_input_selector))).send_keys(LAFTEL_PASSWORD)
        driver.find_element(By.CSS_SELECTOR, "#root > div.sc-4f246076-1.kxlezL > div > div > form > button").click()
        profile_selector = "#root > div.sc-dfccfe31-0.bvOzPl > div > div.sc-f8159b5a-0.fZezog > div:nth-child(1) > div:nth-child(1)"
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, profile_selector))).click()
        wait.until(EC.url_to_be("https://laftel.net/"))
        print("로그인 및 프로필 선택 완료")
        return driver
    except Exception as e:
        print(f"오류: 로그인/프로필 선택 중: {e}")
        if 'driver' in locals():
            driver.quit()
        return None

def get_episode_links_and_title(driver, anime_id):
    # 재생 페이지 목록에서 링크와 함께 제목도 추출
    try:
        item_page_url = f"https://laftel.net/item/{anime_id}"
        print(f"애니메이션 정보 페이지로 이동: {item_page_url}")
        driver.get(item_page_url)
        wait = WebDriverWait(driver, 10)
        
        header_selector = "#item-modal > div.sc-87dbc590-0.cuzTcb > div.sc-87dbc590-1.ikTlOc > div > header"
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, header_selector)))
        
        title_selector = ".sc-b12ebb9a-1"
        anime_title = driver.find_element(By.CSS_SELECTOR, title_selector).text
        sanitized_title = sanitize_filename(anime_title)
        print(f"애니메이션 제목 '{sanitized_title}' 확인")
        
        episode_link_selector = "#item-tab-view > div.sc-8d457a41-1.eNuwum > div > ul > li > a"
        first_episode_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, episode_link_selector)))
        player_page_url = first_episode_element.get_attribute('href')
        
        print(f"전체 에피소드 목록 확인을 위해 재생 페이지로 이동")
        driver.get(player_page_url)
        
        video_player_selector = "#root-video-fullscreen > div > div.sc-aa80e0b2-0.klUxFI"
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, video_player_selector)))
        print("비디오 플레이어 로드 확인")

        player_list_container_selector = "#root > div.sc-4a02fa07-0.cSulJK > div > div.sc-822cc31f-3.cVEuQZ > div > aside > div > div > div.simplebar-wrapper > div.simplebar-mask > div > div > div > ul"
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, player_list_container_selector)))
        print("에피소드 목록 로드 확인")
        
        player_episode_link_selector = f"{player_list_container_selector} > li > a"
        episode_elements = driver.find_elements(By.CSS_SELECTOR, player_episode_link_selector)
        
        links = [element.get_attribute('href') for element in episode_elements]
        print(f"총 {len(links)}개의 에피소드 링크 확보")
        return links, sanitized_title
    except Exception as e:
        print(f"오류: 에피소드 링크/제목 추출 중: {e}")
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
            'N_m3u8DL-RE', mpd_url,
            '--save-name', save_name,
            '--save-dir', str(download_dir.resolve()),
            '-M', 'format=mkv:muxer=mkvmerge',
            '--select-video', 'best',
            '--select-audio', 'best',
            '--no-log'
        ] + key_args
        
        print(f"다운로드 시작: {save_name}.mkv")
        subprocess.run(command)
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
    
    driver = login_and_select_profile_wire()
    if driver:
        episode_links, anime_title = get_episode_links_and_title(driver, ANIME_ID)
        
        DOWNLOAD_DIR = DOWNLOAD_DIR_BASE / anime_title
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        if episode_links and anime_title:
            failed_episodes = []
            total_downloaded_bytes = 0

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
        
        total_gb = total_downloaded_bytes / (1024 ** 3)
        print(f"총 다운로드 용량: {total_gb:.2f} GB")

        if 'driver' in locals() and driver:
            try:
                driver.quit()
            except:
                pass