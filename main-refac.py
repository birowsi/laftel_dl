import os
import re
import subprocess
import time
import json
import pickle
import shutil
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
from selenium.common.exceptions import TimeoutException

# --- 설정 및 상수 ---
ASCII_ART = """
██╗      █████╗ ███████╗████████╗███████╗██╗     
██║     ██╔══██╗██╔════╝╚══██╔══╝██╔════╝██║     
██║     ███████║█████╗     ██║   █████╗  ██║     
██║     ██╔══██║██╔══╝     ██║   ██╔══╝  ██║     
███████╗██║  ██║██║        ██║   ███████╗███████╗
╚══════╝╚═╝  ╚═╝╚═╝        ╚═╝   ╚══════╝╚══════╝
"""

load_dotenv()
ANIME_ID = 40846
WVD_PATH = Path("./license/device.wvd")
DOWNLOAD_DIR_BASE = Path("./downloads")
COOKIE_FILE = Path("laftel_cookies.pkl")

class LaftelDownloader:
    def __init__(self):
        self.email = os.getenv("LAFTEL_EMAIL")
        self.password = os.getenv("LAFTEL_PASSWORD")
        self.driver = None
        self.wait = None

    def setup_driver(self):
        """Selenium Wire 드라이버 초기화"""
        options = {'suppress_connection_errors': True}
        chrome_options = webdriver.ChromeOptions()
        prefs = {"profile.default_content_setting_values.notifications": 2}
        chrome_options.add_experimental_option("prefs", prefs)
        chrome_options.add_argument("--mute-audio")
        # chrome_options.add_argument("--headless") # 필요 시 주석 해제 (단, 탐지 가능성 있음)

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, seleniumwire_options=options, options=chrome_options)
        self.wait = WebDriverWait(self.driver, 15)

    def save_cookies(self):
        """쿠키 저장"""
        with open(COOKIE_FILE, "wb") as f:
            pickle.dump(self.driver.get_cookies(), f)
        print("✅ 쿠키 저장 완료")

    def load_cookies(self):
        """쿠키 로드"""
        if COOKIE_FILE.exists():
            try:
                with open(COOKIE_FILE, "rb") as f:
                    cookies = pickle.load(f)
                    for cookie in cookies:
                        self.driver.add_cookie(cookie)
                print("✅ 쿠키 로드 완료")
                return True
            except Exception as e:
                print(f"⚠️ 쿠키 로드 실패: {e}")
                return False
        return False

    def login(self):
        """로그인 및 프로필 선택 (쿠키 재사용 포함)"""
        if not self.driver:
            self.setup_driver()

        self.driver.get("https://laftel.net/")
        
        # 쿠키가 있으면 적용 후 새로고침하여 로그인 상태 확인
        if self.load_cookies():
            self.driver.refresh()
            time.sleep(2)
            if "auth/login" not in self.driver.current_url:
                 # 이미 로그인 된 상태라면 프로필 선택 화면인지 확인
                try:
                    profile_selector = "#root > div.sc-dfccfe31-0.bvOzPl > div > div.sc-f8159b5a-0.fZezog > div:nth-child(1) > div:nth-child(1)"
                    if len(self.driver.find_elements(By.CSS_SELECTOR, profile_selector)) > 0:
                        self.driver.find_element(By.CSS_SELECTOR, profile_selector).click()
                        self.wait.until(EC.url_to_be("https://laftel.net/"))
                    print("✅ 기존 세션으로 로그인 성공")
                    return
                except:
                    pass

        # 로그인 절차 진행
        print("🔄 로그인을 진행합니다...")
        self.driver.get("https://laftel.net/auth/login")
        
        try:
            # 이메일 로그인 버튼 클릭
            start_email_selector = "#root > div.sc-4f246076-1.kxlezL > div > div > div.sc-755b8e85-5.jFLdwS > div > div.sc-c41aa92c-3.jElMpk > div > button"
            self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, start_email_selector))).click()
            
            # 이메일 입력
            email_input = self.wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[placeholder="이메일 주소"]')))
            email_input.send_keys(self.email)
            self.driver.find_element(By.CSS_SELECTOR, "form > button").click()
            
            # 비밀번호 입력
            pw_input = self.wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, 'input[type="password"]')))
            pw_input.send_keys(self.password)
            self.driver.find_element(By.CSS_SELECTOR, "form > button").click()
            
            # 프로필 선택
            profile_selector = "#root > div.sc-dfccfe31-0.bvOzPl > div > div.sc-f8159b5a-0.fZezog > div:nth-child(1) > div:nth-child(1)"
            self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, profile_selector))).click()
            self.wait.until(EC.url_to_be("https://laftel.net/"))
            
            self.save_cookies() # 로그인 성공 시 쿠키 저장
            print("✅ 로그인 및 프로필 선택 완료")
            
        except Exception as e:
            print(f"❌ 로그인 실패: {e}")
            self.driver.quit()
            raise

    def get_episode_list(self, anime_id):
        """에피소드 링크 및 제목 추출"""
        try:
            url = f"https://laftel.net/item/{anime_id}"
            self.driver.get(url)
            
            # 제목 추출
            title_selector = ".sc-b12ebb9a-1"
            title_el = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, title_selector)))
            title = self.sanitize_filename(title_el.text)
            print(f"🎬 애니메이션: {title}")

            # 첫 화 재생 버튼 찾기 (재생 페이지 진입용)
            play_btn_selector = "#item-tab-view > div.sc-8d457a41-1.eNuwum > div > ul > li > a"
            first_ep_link = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, play_btn_selector))).get_attribute('href')
            
            self.driver.get(first_ep_link)
            
            # 에피소드 목록 로딩 대기
            list_selector = "div.simplebar-content > ul > li > a" # Selector가 변경될 수 있으므로 주의
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, list_selector)))
            
            # 모든 에피소드 링크 수집
            episode_elements = self.driver.find_elements(By.CSS_SELECTOR, list_selector)
            links = [el.get_attribute('href') for el in episode_elements]
            
            print(f"📋 총 {len(links)}개 에피소드 발견")
            return links, title
        except Exception as e:
            print(f"❌ 에피소드 목록 추출 실패: {e}")
            return [], None

    def capture_network_info(self, url):
        """네트워크 요청에서 MPD 및 License 정보 캡처"""
        del self.driver.requests
        self.driver.get(url)
        
        mpd_url = None
        lic_url = None
        lic_headers = None

        try:
            print("📡 네트워크 트래픽 분석 중...", end="\r")
            self.driver.wait_for_request(r'\.mpd', timeout=15)
            self.driver.wait_for_request(r'licenseManager\.do', timeout=15)
            
            for req in self.driver.requests:
                if req.response:
                    if not mpd_url and '.mpd' in req.url:
                        mpd_url = req.url
                    elif 'licenseManager.do' in req.url and req.method == 'POST':
                        lic_url = req.url
                        lic_headers = dict(req.headers)
            
            if mpd_url and lic_url:
                print("✅ 네트워크 정보 획득 완료      ")
                return mpd_url, lic_url, lic_headers
            else:
                print("❌ MPD 또는 License URL을 찾지 못했습니다.")
                return None, None, None
        except TimeoutException:
            print("❌ 네트워크 요청 타임아웃")
            return None, None, None

    def get_pssh(self, mpd_url, headers):
        """init.m4f 다운로드 및 PSSH 추출"""
        # 임시 파일명에 타임스탬프 추가하여 충돌 방지
        temp_init = Path(f"init_{int(time.time())}.m4f")
        
        try:
            cmd = [
                'yt-dlp', '--quiet', '--no-warnings', '--test',
                '--allow-unplayable-formats',
                '-f', 'bestvideo[ext=mp4]',
                '-o', str(temp_init.resolve()),
                mpd_url
            ]
            # 헤더 추가 필요시 cmd.extend(...)
            
            subprocess.run(cmd, check=True, capture_output=True)
            
            if not temp_init.exists():
                return None

            raw_data = temp_init.read_bytes()
            pssh_list = self.extract_pssh_offsets(raw_data)
            
            # 적절한 길이의 PSSH 선택
            for p in pssh_list:
                encoded = base64.b64encode(p).decode()
                if 20 < len(encoded) < 220:
                    return encoded
            return None
            
        except Exception as e:
            print(f"⚠️ PSSH 추출 중 오류: {e}")
            return None
        finally:
            if temp_init.exists():
                temp_init.unlink()

    def extract_pssh_offsets(self, raw: bytes) -> list:
        offsets = []
        offset = 0
        while True:
            offset = raw.find(b'pssh', offset)
            if offset == -1:
                break
            # pssh box size (4 bytes before 'pssh')
            size_bytes = raw[offset-4:offset]
            size = int.from_bytes(size_bytes, byteorder='big')
            
            box_start = offset - 4
            box_end = box_start + size
            
            if box_end <= len(raw):
                offsets.append(raw[box_start:box_end])
            
            offset += size
        return offsets

    def get_decryption_keys(self, pssh, lic_url, headers):
        """Pywidevine을 이용한 키 발급"""
        try:
            device = Device.load(WVD_PATH)
            cdm = Cdm.from_device(device)
            session_id = cdm.open()
            
            challenge = cdm.get_license_challenge(session_id, PSSH(pssh))
            
            # 헤더 정리 (불필요한 헤더 제외 및 필요한 헤더 확보)
            req_headers = {
                "pallycon-customdata-v2": headers.get('pallycon-customdata-v2', ''),
                "User-Agent": headers.get('User-Agent', ''),
                "Content-Type": "application/octet-stream"
            }
            
            # httpx를 사용하여 라이선스 요청
            with httpx.Client(http2=True) as client:
                resp = client.post(lic_url, data=challenge, headers=req_headers)
                resp.raise_for_status()
                
            cdm.parse_license(session_id, resp.content)
            
            key_strings = []
            for key in cdm.get_keys(session_id):
                if key.type == 'CONTENT':
                    key_strings.append(f"{key.kid.hex}:{key.key.hex()}")
            
            cdm.close(session_id)
            return key_strings
        except Exception as e:
            print(f"❌ 키 발급 오류: {e}")
            return None

    def download_video(self, mpd_url, keys, filename, save_dir):
        """N_m3u8DL-RE 호출"""
        save_path = save_dir / f"{filename}.mkv"
        if save_path.exists():
            print(f"⏭️  이미 존재함: {filename}")
            return True

        key_args = []
        for k in keys:
            key_args.extend(['--key', k])

        cmd = [
            'N_m3u8DL-RE', mpd_url,
            '--save-name', filename,
            '--save-dir', str(save_dir),
            '-M', 'format=mkv:muxer=mkvmerge',
            '--select-video', 'best',
            '--select-audio', 'best',
            '--no-log',
            '--log-level', 'OFF'
        ] + key_args

        print(f"⬇️  다운로드 시작: {filename}")
        result = subprocess.run(cmd, capture_output=False) # 로그를 보려면 capture_output 제거
        
        if result.returncode == 0:
            print(f"✅ 다운로드 완료: {filename}")
            return True
        else:
            print(f"❌ 다운로드 실패: {filename}")
            return False

    @staticmethod
    def sanitize_filename(name):
        return re.sub(r'[\\/*?:"<>|]', "", name).strip()[:200] # 길이 제한 추가

    def run(self):
        print(ASCII_ART)
        
        try:
            self.login()
            links, title = self.get_episode_list(ANIME_ID)
            
            if not links:
                print("❌ 에피소드를 찾을 수 없습니다.")
                return

            save_dir = DOWNLOAD_DIR_BASE / title
            save_dir.mkdir(parents=True, exist_ok=True)

            print(f"\n🚀 '{title}' 다운로드 시작 (총 {len(links)}화)")
            
            failed_episodes = []

            for idx, link in enumerate(links):
                ep_num = idx + 1
                filename = f"{title} {ep_num}화"
                print(f"\n--- [{ep_num}/{len(links)}] 처리 중 ---")
                
                # 네트워크 캡처
                mpd, lic, headers = self.capture_network_info(link)
                if not mpd:
                    failed_episodes.append(ep_num)
                    continue
                
                # PSSH 추출
                pssh = self.get_pssh(mpd, headers)
                if not pssh:
                    print("❌ PSSH 추출 실패")
                    failed_episodes.append(ep_num)
                    continue
                
                # 키 발급
                keys = self.get_decryption_keys(pssh, lic, headers)
                if not keys:
                    print("❌ 키 발급 실패")
                    failed_episodes.append(ep_num)
                    continue
                
                print(f"🔑 Key: {keys[0]} ...")
                
                # 다운로드
                if not self.download_video(mpd, keys, filename, save_dir):
                    failed_episodes.append(ep_num)

            if failed_episodes:
                print(f"\n⚠️ 실패한 에피소드: {failed_episodes}")
            else:
                print("\n🎉 모든 다운로드 완료!")

        except Exception as e:
            print(f"\n❌ 치명적 오류 발생: {e}")
        finally:
            if self.driver:
                self.driver.quit()

if __name__ == "__main__":
    downloader = LaftelDownloader()
    downloader.run()