# FILE: browser_session.py
# AI_NOTE: Browser/session module. Owns webdriver creation, session verification, and login flow transitions (including optional off-screen visible mode).
import re
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from runtime_support import (
    LOGIN_WAIT_TIMEOUT_SEC,
    _write_driver_pid,
    cleanup_profile_locked_chrome,
    cleanup_stale_driver_process,
    log_print as print,
    safe_quit_driver,
)


DEFAULT_ANIME_ID = 40846


def _driver_get_safe(driver, url: str) -> bool:
    try:
        driver.get(url)
        return True
    except Exception as e:
        print(f"경고: 브라우저 이동 실패: {type(e).__name__}: {e}")
        return False


def is_target_player_link(href: str, anime_id: int) -> bool:
    if not href:
        return False
    match = re.search(r"/player/(\d+)/(\d+)", href)
    if not match:
        return False
    return int(match.group(1)) == int(anime_id)


def is_login_url(url):
    return "/auth/" in (url or "").lower()


def is_home_url(url):
    return (url or "").lower().rstrip("/") == "https://laftel.net"


def has_authenticated_player_access(driver, anime_id=DEFAULT_ANIME_ID):
    original_url = None
    try:
        try:
            original_url = driver.current_url
        except Exception:
            original_url = None
        item_url = f"https://laftel.net/item/{anime_id}"
        if not _driver_get_safe(driver, item_url):
            return False
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

        if not _driver_get_safe(driver, player_url):
            return False
        time.sleep(1)
        if is_login_url(driver.current_url):
            return False

        login_required_markers = driver.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.), '작품 감상을 위해 로그인이 필요해요.')]",
        )
        if login_required_markers:
            return False
        login_buttons = driver.find_elements(
            By.XPATH,
            "//a[contains(normalize-space(.), '로그인') or contains(@href, '/auth/login')]"
            " | //button[contains(normalize-space(.), '로그인')]",
        )
        if login_buttons:
            return False

        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#root-video-fullscreen")))
            return True
        except Exception:
            return ("/player/" in (driver.current_url or "")) and (not is_login_url(driver.current_url))
    except Exception:
        return False
    finally:
        try:
            if original_url and not is_login_url(original_url):
                _driver_get_safe(driver, original_url)
        except BaseException:
            pass


def create_webdriver_with_profile(headless=False, offscreen=False):
    cleanup_stale_driver_process()
    chrome_options = webdriver.ChromeOptions()
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    chrome_options.add_experimental_option(
        "prefs",
        {"profile.default_content_setting_values.notifications": 2},
    )
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--autoplay-policy=no-user-gesture-required")
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
    elif offscreen:
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--window-position=-32000,-32000")

    profile_dir = Path("./.chrome-profile").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    chrome_options.add_argument("--profile-directory=Default")

    cleanup_profile_locked_chrome()
    service = Service(ChromeDriverManager().install())
    try:
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        print(f"경고: 크롬 드라이버 초기 실행 실패, 복구 후 재시도합니다: {type(e).__name__}")
        cleanup_stale_driver_process()
        cleanup_profile_locked_chrome()
        time.sleep(1)
        driver = webdriver.Chrome(service=service, options=chrome_options)

    service_process = getattr(getattr(driver, "service", None), "process", None)
    service_pid = getattr(service_process, "pid", None)
    if service_pid:
        _write_driver_pid(service_pid)
    return driver


def ensure_logged_in(driver, anime_id=DEFAULT_ANIME_ID):
    try:
        if has_authenticated_player_access(driver, anime_id=anime_id):
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
            if is_login_url(current_url):
                time.sleep(1)
                continue

            if not is_home_url(current_url):
                time.sleep(1)
                continue

            if has_authenticated_player_access(driver, anime_id=anime_id):
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


def login_and_select_profile_wire(anime_id=DEFAULT_ANIME_ID, offscreen=False):
    driver = create_webdriver_with_profile(headless=False, offscreen=offscreen)
    if ensure_logged_in(driver, anime_id=anime_id):
        return driver
    safe_quit_driver(driver)
    return None


def recreate_driver_headless(existing_driver, anime_id=DEFAULT_ANIME_ID):
    safe_quit_driver(existing_driver)
    driver = create_webdriver_with_profile(headless=True)
    if not has_authenticated_player_access(driver, anime_id=anime_id):
        safe_quit_driver(driver)
        return None
    return driver


def get_headless_driver_if_session_exists(anime_id=DEFAULT_ANIME_ID):
    headless = create_webdriver_with_profile(headless=True)
    if has_authenticated_player_access(headless, anime_id=anime_id):
        return headless
    safe_quit_driver(headless)
    return None


def get_or_login_headless_driver(anime_id=DEFAULT_ANIME_ID):
    headless = create_webdriver_with_profile(headless=True)
    try:
        if has_authenticated_player_access(headless, anime_id=anime_id):
            return headless
    except Exception as e:
        print(f"경고: 헤드리스 세션 확인 중 예외 발생: {type(e).__name__}: {e}")
    safe_quit_driver(headless)

    visible = create_webdriver_with_profile(headless=False)
    try:
        if not ensure_logged_in(visible, anime_id=anime_id):
            safe_quit_driver(visible)
            return None
        return recreate_driver_headless(visible, anime_id=anime_id)
    except Exception as e:
        print(f"오류: 로그인 드라이버 처리 중 예외 발생: {type(e).__name__}: {e}")
        safe_quit_driver(visible)
        return None
