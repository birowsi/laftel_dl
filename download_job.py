# FILE: download_job.py
# AI_NOTE: Download executor class with optional event hooks and episode-range filtering. Resolves episodes, captures DRM/network data, auto-recovers broken webdriver sessions, and runs retry logic on offscreen-visible browser sessions.
import os
import re
import subprocess
import time
import json
import locale
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from browser_session import (
    is_captcha_challenge_visible,
    is_target_player_link,
    login_and_select_profile_wire,
)
from drm_support import get_key_original, get_pssh_from_init
from runtime_support import (
    N_M3U8DL_RE_EXE,
    REQUEST_TIMEOUT_SEC,
    _clear_download_marker,
    _write_download_marker,
    build_process_env,
    log_print as print,
    normalize_anime_title,
    safe_quit_driver,
    sanitize_filename,
)


@dataclass
class EpisodeResult:
    success: bool
    downloaded_bytes: int = 0
    reason: str | None = None
    retriable: bool = False


@dataclass
class NetworkCaptureState:
    mpd_url: str | None = None
    lic_url: str | None = None
    lic_headers: dict[str, Any] | None = None
    request_url_by_id: dict[str, str] = field(default_factory=dict)


@dataclass
class EpisodeEntry:
    href: str
    episode_num: int | None = None
    label: str = ""
    order_num: int = 0


class DownloadJob:
    LINK_POLL_INTERVAL_SEC = 0.5
    NETWORK_POLL_INTERVAL_SEC = 0.2
    LICENSE_PROBE_LOG_INTERVAL_SEC = 5
    PLAYER_TRIGGER_SCRIPT_TIMEOUT_SEC = 5

    def __init__(
        self,
        driver,
        anime_id,
        max_retries=5,
        should_stop=None,
        on_event=None,
        episode_selection=None,
    ):
        self.driver = driver
        self.anime_id = anime_id
        self.max_retries = max_retries
        self.should_stop = should_stop
        self.on_event = on_event
        self.episode_selection = episode_selection
        self.download_dir_base = Path("./downloads")
        self._captcha_detected = False

    def _stopped(self):
        return bool(self.should_stop and self.should_stop())

    def _emit(self, event, **payload):
        if not self.on_event:
            return
        try:
            self.on_event({"event": event, **payload})
        except Exception:
            # 이벤트 훅 오류가 다운로드 본 흐름을 깨지 않도록 격리한다.
            pass

    @staticmethod
    def _is_driver_disconnected_exception(exc: Exception) -> bool:
        if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException)):
            return True
        text = str(exc).lower()
        return (
            "invalid session id" in text
            or "session deleted as the browser has closed the connection" in text
            or "not connected to devtools" in text
            or "target window already closed" in text
        )

    def _recover_driver_session(self, episode_num: int) -> bool:
        print(f"경고: {episode_num}화 처리 전 브라우저 세션 끊김 감지. 드라이버를 재생성합니다.")
        self._emit("driver_recover_start", episode_num=episode_num)
        safe_quit_driver(self.driver)
        self.driver = login_and_select_profile_wire(anime_id=self.anime_id, offscreen=True)
        if not self.driver:
            print("오류: 드라이버 세션 복구 실패")
            self._emit("driver_recover_failed", episode_num=episode_num)
            return False
        print("드라이버 세션 복구 완료")
        self._emit("driver_recover_done", episode_num=episode_num)
        return True

    @staticmethod
    def _retriable_reason(reason: str) -> bool:
        retriable_reasons = {
            "network_data_missing",
            "pssh_missing",
            "key_missing",
            "driver_reconnect_failed",
            "downloader_nonzero_exit",
            "unexpected_exception",
        }
        return reason in retriable_reasons

    @staticmethod
    def parse_episode_selection(selection_text: str):
        if selection_text is None:
            return None
        text = str(selection_text).strip()
        if not text:
            return None

        selected = set()
        tokens = [token.strip() for token in text.split(",") if token.strip()]
        if not tokens:
            return None

        for token in tokens:
            if "-" in token:
                parts = [p.strip() for p in token.split("-", 1)]
                if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                    raise ValueError(f"잘못된 회차 범위 형식: {token}")
                start = int(parts[0])
                end = int(parts[1])
                if start <= 0 or end <= 0:
                    raise ValueError(f"회차는 1 이상이어야 합니다: {token}")
                if start > end:
                    raise ValueError(f"시작 회차가 종료 회차보다 클 수 없습니다: {token}")
                for num in range(start, end + 1):
                    selected.add(num)
            else:
                if not token.isdigit():
                    raise ValueError(f"잘못된 회차 형식: {token}")
                num = int(token)
                if num <= 0:
                    raise ValueError(f"회차는 1 이상이어야 합니다: {token}")
                selected.add(num)

        return selected if selected else None

    @staticmethod
    def _extract_episode_number_from_text(text: str) -> int | None:
        match = re.search(r"(\d+)\s*화", text or "")
        if match:
            return int(match.group(1))
        return None

    def _collect_player_entries_with_wait(self, selectors, timeout=20, min_count=1):
        deadline = time.time() + timeout
        found = []
        while time.time() < deadline:
            entries = []
            for selector in selectors:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    href = element.get_attribute("href")
                    if not is_target_player_link(href, self.anime_id):
                        continue
                    label = (
                        element.text
                        or element.get_attribute("title")
                        or element.get_attribute("aria-label")
                        or ""
                    ).strip()
                    if href not in [item.href for item in entries]:
                        entries.append(
                            EpisodeEntry(
                                href=href,
                                label=label,
                                episode_num=self._extract_episode_number_from_text(label),
                                order_num=len(entries) + 1,
                            )
                        )
            if len(entries) >= min_count:
                return entries
            found = entries
            time.sleep(self.LINK_POLL_INTERVAL_SEC)
        return found

    def get_episode_links_and_title(self):
        try:
            item_page_url = f"https://laftel.net/item/{self.anime_id}"
            print(f"애니메이션 정보 페이지로 이동: {item_page_url}")
            self.driver.get(item_page_url)
            wait = WebDriverWait(self.driver, 20)

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

            normalized_title = normalize_anime_title(anime_title)
            sanitized_title = sanitize_filename(normalized_title)
            if not sanitized_title:
                sanitized_title = sanitize_filename(anime_title)
            print(f"애니메이션 제목 '{sanitized_title}' 확인")

            entries = self._collect_player_entries_with_wait(
                selectors=["#item-tab-view a[href*='/player/']", "a[href*='/player/']"],
                timeout=20,
                min_count=2,
            )
            numbered_entries = [item for item in entries if item.episode_num is not None]
            if len(numbered_entries) == len(entries) and numbered_entries:
                numbered_entries.sort(key=lambda item: item.episode_num)
                entries = numbered_entries
            links = [item.href for item in entries]

            if len(links) > 1:
                print("item 페이지에서 에피소드 목록 로드 확인")
                print(f"총 {len(links)}개의 에피소드 링크 확보")
                return entries, sanitized_title

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
            self.driver.get(player_page_url)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#root-video-fullscreen")))
            print("비디오 플레이어 로드 확인")

            entries = self._collect_player_entries_with_wait(
                selectors=["aside a[href*='/player/']", "a[href*='/player/']"],
                timeout=25,
                min_count=2,
            )
            numbered_entries = [item for item in entries if item.episode_num is not None]
            if len(numbered_entries) == len(entries) and numbered_entries:
                numbered_entries.sort(key=lambda item: item.episode_num)
                entries = numbered_entries
            links = [item.href for item in entries]

            if not links:
                for match in re.findall(r"https://laftel\.net/player/\d+/\d+", self.driver.page_source):
                    if is_target_player_link(match, self.anime_id) and match not in links:
                        links.append(match)
                if links:
                    # Fallback으로 링크를 찾은 경우에도 반환 타입을 EpisodeEntry로 맞춘다.
                    entries = [
                        EpisodeEntry(
                            href=href,
                            episode_num=None,
                            label="",
                            order_num=index + 1,
                        )
                        for index, href in enumerate(links)
                    ]

            print("에피소드 목록 로드 확인")
            print(f"총 {len(links)}개의 에피소드 링크 확보")
            return entries, sanitized_title
        except Exception as e:
            print(f"오류: 에피소드 링크/제목 추출 중: {type(e).__name__}: {e}")
            try:
                print(f"현재 URL: {self.driver.current_url}")
            except Exception:
                pass
            return [], None

    @staticmethod
    def _is_license_like_request(url_lower: str, headers_lower: dict[str, Any] | None = None) -> bool:
        header_map = headers_lower or {}
        return (
            ("pallycon" in url_lower and "license" in url_lower)
            or "licensemanager.do" in url_lower
            or "pallycon-customdata-v2" in header_map
        )

    @staticmethod
    def _shorten_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        except Exception:
            return url

    def _drain_performance_events(self):
        try:
            perf_logs = self.driver.get_log("performance")
        except Exception:
            return []

        events = []
        for entry in perf_logs:
            try:
                message = json.loads(entry["message"])["message"]
            except Exception:
                continue
            events.append(message)
        return events

    def _install_request_probe(self):
        try:
            self.driver.execute_script(
                """
                if (!window.__laftelProbeInstalled) {
                  window.__laftelProbeInstalled = true;
                  window.__laftelProbeLogs = [];

                  const pushLog = (url, method, headers) => {
                    try {
                      window.__laftelProbeLogs.push({
                        url: url || "",
                        method: method || "",
                        headers: headers || {},
                        ts: Date.now()
                      });
                      if (window.__laftelProbeLogs.length > 500) {
                        window.__laftelProbeLogs = window.__laftelProbeLogs.slice(-300);
                      }
                    } catch (_) {}
                  };

                  const normalizeHeaders = (headers) => {
                    const out = {};
                    if (!headers) return out;
                    if (typeof Headers !== "undefined" && headers instanceof Headers) {
                      headers.forEach((v, k) => { out[String(k).toLowerCase()] = v; });
                      return out;
                    }
                    if (Array.isArray(headers)) {
                      headers.forEach((pair) => {
                        if (Array.isArray(pair) && pair.length >= 2) {
                          out[String(pair[0]).toLowerCase()] = pair[1];
                        }
                      });
                      return out;
                    }
                    if (typeof headers === "object") {
                      Object.keys(headers).forEach((k) => {
                        out[String(k).toLowerCase()] = headers[k];
                      });
                    }
                    return out;
                  };

                  const origFetch = window.fetch;
                  if (origFetch) {
                    window.fetch = function(input, init) {
                      try {
                        const url = (typeof input === "string") ? input : (input && input.url) || "";
                        const method = (init && init.method) || (input && input.method) || "GET";
                        const headers = normalizeHeaders((init && init.headers) || (input && input.headers));
                        pushLog(url, method, headers);
                      } catch (_) {}
                      return origFetch.apply(this, arguments);
                    };
                  }

                  const origOpen = XMLHttpRequest.prototype.open;
                  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
                  const origSend = XMLHttpRequest.prototype.send;

                  XMLHttpRequest.prototype.open = function(method, url) {
                    this.__lf_method = method;
                    this.__lf_url = url;
                    this.__lf_headers = {};
                    return origOpen.apply(this, arguments);
                  };
                  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
                    try {
                      this.__lf_headers = this.__lf_headers || {};
                      this.__lf_headers[String(name).toLowerCase()] = value;
                    } catch (_) {}
                    return origSetHeader.apply(this, arguments);
                  };
                  XMLHttpRequest.prototype.send = function() {
                    try {
                      pushLog(this.__lf_url, this.__lf_method || "GET", this.__lf_headers || {});
                    } catch (_) {}
                    return origSend.apply(this, arguments);
                  };
                }
                """
            )
        except Exception:
            pass

    def _collect_license_from_probe(self):
        try:
            logs = self.driver.execute_script("return (window.__laftelProbeLogs || []).slice(-300);") or []
        except Exception:
            return None, None, 0, []

        urls = []
        for row in reversed(logs):
            url = str(row.get("url") or "")
            if not url:
                continue
            urls.append(url)
            headers = row.get("headers") or {}
            headers_lower = {str(k).lower(): v for k, v in headers.items()}
            if self._is_license_like_request(url.lower(), headers_lower):
                return url, headers, len(logs), urls
        return None, None, len(logs), urls

    def _ingest_events(self, events, state: NetworkCaptureState):
        for msg in events:
            method = msg.get("method")
            params = msg.get("params", {})

            if method == "Network.requestWillBeSent":
                req_id = params.get("requestId")
                request = params.get("request", {})
                url = request.get("url") or ""
                if not url:
                    continue
                url_lower = url.lower()
                headers = request.get("headers") or {}
                headers_lower = {str(k).lower(): v for k, v in headers.items()}
                if req_id:
                    state.request_url_by_id[req_id] = url

                if ".mpd" in url_lower and not state.mpd_url:
                    state.mpd_url = url

                if self._is_license_like_request(url_lower, headers_lower):
                    state.lic_url = state.lic_url or url
                    if headers:
                        state.lic_headers = state.lic_headers or headers

            elif method == "Network.requestWillBeSentExtraInfo":
                req_id = params.get("requestId")
                headers = params.get("headers") or {}
                headers_lower = {str(k).lower(): v for k, v in headers.items()}
                if "pallycon-customdata-v2" in headers_lower:
                    state.lic_headers = headers
                    if not state.lic_url and req_id:
                        state.lic_url = state.request_url_by_id.get(req_id)

    def _trigger_player_activity(self):
        try:
            self.driver.set_script_timeout(self.PLAYER_TRIGGER_SCRIPT_TIMEOUT_SEC)
            self.driver.execute_async_script(
                """
                const done = arguments[arguments.length - 1];
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
                done('ok');
                """
            )
            print("재생 트리거 호출 완료")
        except TimeoutException:
            print(f"경고: 재생 트리거 스크립트 타임아웃 ({self.PLAYER_TRIGGER_SCRIPT_TIMEOUT_SEC}초)")
        except WebDriverException as e:
            print(f"경고: 재생 트리거 WebDriver 예외: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"경고: 재생 트리거 예외: {type(e).__name__}: {e}")

    def get_network_data(self, episode_url, retries=0, wait_sec=None, soft_fail=False):
        print(f"\n네트워크 감시 시작: {episode_url}")
        print("네트워크 감시 모드: Selenium CDP performance log")
        timeout_sec = int(wait_sec or REQUEST_TIMEOUT_SEC)

        attempt = 0
        while attempt <= retries:
            try:
                print(f"네트워크 감시 시도 {attempt + 1}/{retries + 1}")
                try:
                    # 직전 요청 로그를 비워 새 회차 요청만 본다.
                    self.driver.get_log("performance")
                except Exception:
                    pass
                self.driver.get(episode_url)
                time.sleep(1)
                if is_captcha_challenge_visible(self.driver):
                    self._captcha_detected = True
                    print("오류: 플레이어 접근 중 캡차/봇 확인 페이지가 감지되었습니다. 캡차를 완료한 뒤 다시 시도해 주세요.")
                    return None, None, None
                self._install_request_probe()

                state = NetworkCaptureState()

                print("MPD 요청 대기 중...")
                mpd_deadline = time.time() + timeout_sec
                while time.time() < mpd_deadline:
                    self._ingest_events(self._drain_performance_events(), state)
                    if state.mpd_url:
                        print("MPD 요청 감지")
                        break
                    time.sleep(self.NETWORK_POLL_INTERVAL_SEC)
                else:
                    raise TimeoutError("MPD 요청 타임아웃")

                print("라이선스 요청 대기 중...")
                print("재생 트리거 시도 (video.play / 재생 버튼 클릭)")
                self._trigger_player_activity()

                lic_deadline = time.time() + timeout_sec
                next_probe_log_at = 0.0
                last_probe_urls = []
                while time.time() < lic_deadline:
                    if is_captcha_challenge_visible(self.driver):
                        self._captcha_detected = True
                        print("오류: 라이선스 감시 중 캡차/봇 확인 페이지가 감지되었습니다.")
                        return None, None, None
                    lic_url, lic_headers, probe_count, probe_urls = self._collect_license_from_probe()
                    if probe_urls:
                        last_probe_urls = probe_urls
                    if lic_url and lic_headers:
                        state.lic_url = lic_url
                        state.lic_headers = lic_headers
                    if state.mpd_url and state.lic_url and state.lic_headers:
                        print("라이선스 요청 감지")
                        return state.mpd_url, state.lic_url, state.lic_headers
                    now = time.time()
                    if now >= next_probe_log_at:
                        print(f"라이선스 프로브 확인 중... (captured={probe_count})")
                        next_probe_log_at = now + self.LICENSE_PROBE_LOG_INTERVAL_SEC
                    time.sleep(self.NETWORK_POLL_INTERVAL_SEC)
                recent_urls = (
                    ", ".join(self._shorten_url(u) for u in list(dict.fromkeys(last_probe_urls))[:5])
                    if last_probe_urls
                    else "none"
                )
                raise TimeoutError(
                    f"라이선스 요청 타임아웃 (mpd={bool(state.mpd_url)}, "
                    f"lic_url={bool(state.lic_url)}, lic_headers={bool(state.lic_headers)}, "
                    f"recent_urls={recent_urls})"
                )
            except Exception as e:
                attempt += 1
                if soft_fail and isinstance(e, TimeoutError):
                    print("경고: 1차 라이선스 요청 감지에 실패했습니다. 즉시 재시도합니다.")
                else:
                    print(f"경고: 네트워크 요청 처리 중: {e}")
                if attempt <= retries:
                    print(f"네트워크 요청 재시도 ({attempt}/{retries})")
                    time.sleep(1)
                else:
                    return None, None, None

    def _retry_wait_seconds(self, retry_pass: int, prior_reason: str | None) -> int:
        base_wait = REQUEST_TIMEOUT_SEC
        if retry_pass <= 0:
            return base_wait
        if prior_reason == "network_data_missing":
            return REQUEST_TIMEOUT_SEC + min(retry_pass * 15, 30)
        return base_wait

    def _run_downloader_with_progress(self, command, env, episode_num: int):
        encoding = locale.getpreferredencoding(False) or "utf-8"
        progress_re = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
        last_ui_percent = 75

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=encoding,
            errors="replace",
            env=env,
        )
        try:
            while True:
                line = process.stdout.readline() if process.stdout else ""
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue

                stripped = line.strip()
                if not stripped:
                    continue

                matches = [float(m.group(1)) for m in progress_re.finditer(stripped)]
                if not matches:
                    continue

                raw_percent = max(0.0, min(max(matches), 100.0))
                # 다운로드 단계 바는 75~99를 사용해 "실제 다운로드 중" 체감을 높인다.
                mapped_percent = 75 + int(raw_percent * 0.24)
                mapped_percent = max(75, min(mapped_percent, 99))
                if mapped_percent > last_ui_percent:
                    last_ui_percent = mapped_percent
                    self._emit(
                        "episode_stage",
                        episode_num=episode_num,
                        stage="download",
                        percent=last_ui_percent,
                    )

            return_code = process.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(returncode=return_code, cmd=command)
        finally:
            if process.stdout:
                process.stdout.close()

    def download_episode(self, link, episode_num, anime_title, download_dir, retry_pass=0, prior_reason=None):
        save_name = f"{anime_title} {episode_num}화"
        expected_filepath = download_dir / f"{save_name}.mkv"
        if expected_filepath.exists():
            print(f"\n이미 파일이 존재하여 건너뜁니다: {expected_filepath.name}")
            self._emit("episode_skipped", episode_num=episode_num, reason="exists", filename=expected_filepath.name)
            return EpisodeResult(success=True, downloaded_bytes=0)

        print(f"\n{'=' * 20} {episode_num}화 처리 시작 {'=' * 20}")
        self._emit("episode_start", episode_num=episode_num, link=link)

        try:
            self.driver.get("about:blank")
        except Exception as e:
            if self._is_driver_disconnected_exception(e):
                if not self._recover_driver_session(episode_num):
                    return EpisodeResult(success=False, reason="driver_reconnect_failed", retriable=True)
                try:
                    self.driver.get("about:blank")
                except Exception as e2:
                    print(f"오류: {episode_num}화 세션 복구 후 초기화 실패: {type(e2).__name__}: {e2}")
                    self._emit(
                        "episode_error",
                        episode_num=episode_num,
                        reason="driver_reconnect_failed",
                        retriable=True,
                    )
                    return EpisodeResult(success=False, reason="driver_reconnect_failed", retriable=True)
            else:
                raise

        time.sleep(1)

        # first try: 빠르게 1회 탐지하고 실패하면 즉시 모드 전환 판단
        initial_wait_sec = self._retry_wait_seconds(retry_pass, prior_reason)
        network_retries = 1
        if retry_pass > 0 and prior_reason == "network_data_missing":
            print(
                f"재시도 보강 적용: {episode_num}화 | retry_pass={retry_pass} | "
                f"wait={initial_wait_sec}s | probe_retries={network_retries}"
            )
        self._emit("episode_stage", episode_num=episode_num, stage="network", percent=15)
        mpd_url, lic_url, lic_headers = self.get_network_data(
            link,
            retries=network_retries,
            wait_sec=initial_wait_sec,
            soft_fail=False,
        )

        if not all([mpd_url, lic_url, lic_headers]):
            print(f"오류: {episode_num}화 네트워크 정보 확보 실패")
            self._emit(
                "episode_error",
                episode_num=episode_num,
                reason="network_data_missing",
                retriable=True,
            )
            return EpisodeResult(success=False, reason="network_data_missing", retriable=True)

        try:
            env = build_process_env()
            print(f"실행 PATH 헤드: {env['PATH'].split(os.pathsep)[0]}")
            self._emit("episode_stage", episode_num=episode_num, stage="pssh", percent=35)
            pssh = get_pssh_from_init(mpd_url, lic_headers)
            if not pssh:
                print(f"오류: {episode_num}화 PSSH 확보 실패")
                self._emit("episode_error", episode_num=episode_num, reason="pssh_missing", retriable=True)
                return EpisodeResult(success=False, reason="pssh_missing", retriable=True)

            self._emit("episode_stage", episode_num=episode_num, stage="keys", percent=55)
            keys = get_key_original(pssh, lic_url, lic_headers)
            if not keys:
                print(f"오류: {episode_num}화 키 추출 실패")
                self._emit("episode_error", episode_num=episode_num, reason="key_missing", retriable=True)
                return EpisodeResult(success=False, reason="key_missing", retriable=True)

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
                str(N_M3U8DL_RE_EXE),
                mpd_url,
                "--save-name",
                save_name,
                "--save-dir",
                str(download_dir.resolve()),
                "-M",
                "format=mkv:muxer=mkvmerge",
                "--select-video",
                "best",
                "--select-audio",
                "best",
                "--no-log",
            ] + key_args

            print(f"다운로드 시작: {save_name}.mkv")
            self._emit("episode_stage", episode_num=episode_num, stage="download", percent=75)
            self._run_downloader_with_progress(command, env, episode_num=episode_num)
            _clear_download_marker()
            print(f"{save_name}.mkv 다운로드 완료")
            downloaded_size = expected_filepath.stat().st_size if expected_filepath.exists() else 0
            self._emit(
                "episode_done",
                episode_num=episode_num,
                success=True,
                downloaded_bytes=downloaded_size,
                filename=expected_filepath.name,
            )
            return EpisodeResult(success=True, downloaded_bytes=downloaded_size)
        except subprocess.CalledProcessError as e:
            reason = "downloader_nonzero_exit"
            retriable = self._retriable_reason(reason)
            print(f"오류: {episode_num}화 다운로드 도구 비정상 종료 (exit={e.returncode})")
            self._emit(
                "episode_error",
                episode_num=episode_num,
                reason=reason,
                retriable=retriable,
                detail=f"exit={e.returncode}",
            )
            return EpisodeResult(success=False, reason=reason, retriable=retriable)
        except Exception as e:
            print(f"오류: {episode_num}화 처리 중 예외 발생: {e}")
            reason = "unexpected_exception"
            retriable = self._retriable_reason(reason)
            self._emit("episode_error", episode_num=episode_num, reason=reason, retriable=retriable, detail=str(e))
            return EpisodeResult(success=False, reason=reason, retriable=retriable)

    def run(self):
        self._emit("job_start", anime_id=self.anime_id, max_retries=self.max_retries)
        episode_entries, anime_title = self.get_episode_links_and_title()
        if not anime_title:
            self._emit("job_error", reason="title_missing")
            raise RuntimeError("애니메이션 제목을 가져오지 못해 작업을 중단합니다.")

        selected_episodes = self.parse_episode_selection(self.episode_selection)
        if selected_episodes:
            numbered_entries = [entry for entry in episode_entries if entry.episode_num is not None]
            if numbered_entries and len(numbered_entries) == len(episode_entries):
                episode_entries = [entry for entry in episode_entries if entry.episode_num in selected_episodes]
            else:
                episode_entries = [entry for entry in episode_entries if entry.order_num in selected_episodes]
            self._emit(
                "episode_list_filtered",
                selected_count=len(episode_entries),
                selection=self.episode_selection,
            )
            print(f"회차 필터 적용: '{self.episode_selection}' -> {len(episode_entries)}개 선택됨")
            print(f"선택된 회차: {', '.join(str(num) for num in sorted(selected_episodes))}")
            mapped = [
                f"{entry.episode_num if entry.episode_num is not None else entry.order_num}화->{entry.href.rsplit('/', 1)[-1]}"
                for entry in episode_entries
            ]
            print(f"실제 매핑: {', '.join(mapped) if mapped else '(없음)'}")
            if not episode_entries:
                raise RuntimeError("회차 필터 결과가 비어 있습니다. 회차 범위를 확인해 주세요.")

        download_dir = self.download_dir_base / anime_title
        download_dir.mkdir(parents=True, exist_ok=True)

        total_downloaded_bytes = 0
        retriable_failures = []
        final_failures = []

        if episode_entries:
            print(f"\n{'=' * 20} 1차 다운로드를 시작합니다 ({len(episode_entries)}개) {'=' * 20}")
            self._emit("episode_list_ready", count=len(episode_entries), anime_title=anime_title)
            for i, entry in enumerate(episode_entries):
                if self._captcha_detected:
                    print("중단: 캡차/봇 확인 페이지가 감지되어 남은 회차 처리를 중단합니다.")
                    break
                display_episode_num = entry.episode_num if entry.episode_num is not None else entry.order_num
                if self._stopped():
                    print("중단 요청 감지: 현재 작업을 종료합니다.")
                    self._emit("job_stop_requested", phase="initial_pass", episode_num=display_episode_num)
                    break
                result = self.download_episode(entry.href, display_episode_num, anime_title, download_dir)
                if result.success:
                    total_downloaded_bytes += result.downloaded_bytes
                else:
                    failure_item = {"entry": entry, "num": display_episode_num, "reason": result.reason}
                    if result.retriable:
                        retriable_failures.append(failure_item)
                    else:
                        final_failures.append(failure_item)
                    print(f"회차 실패 분류: {display_episode_num}화 | reason={result.reason} | retriable={result.retriable}")
        else:
            print("다운로드 가능한 에피소드 링크를 찾지 못했습니다.")

        retry_pass = 0
        while retriable_failures and retry_pass < self.max_retries:
            if self._captcha_detected:
                print("중단: 캡차/봇 확인 페이지가 감지되어 재시도를 시작하지 않습니다.")
                break
            if self._stopped():
                print("중단 요청 감지: 재시도를 시작하지 않습니다.")
                self._emit("job_stop_requested", phase="retry_wait")
                break
            retry_pass += 1
            print(f"\n{'=' * 20} 실패한 {len(retriable_failures)}개 항목 재시도 ({retry_pass}/{self.max_retries}) {'=' * 20}")
            self._emit("retry_pass_start", retry_pass=retry_pass, failed_count=len(retriable_failures))
            print(
                "재시도 대상: "
                + ", ".join(f"{episode['num']}화({episode['reason']})" for episode in retriable_failures)
            )

            safe_quit_driver(self.driver)
            self.driver = login_and_select_profile_wire(anime_id=self.anime_id, offscreen=True)
            if not self.driver:
                print("오류: 재로그인 실패. 재시도를 중단합니다.")
                self._emit("job_error", reason="relogin_failed", retry_pass=retry_pass)
                break

            failures_this_pass = []
            for episode in retriable_failures:
                if self._stopped():
                    print("중단 요청 감지: 재시도 루프를 종료합니다.")
                    self._emit("job_stop_requested", phase="retry_loop", retry_pass=retry_pass)
                    break
                result = self.download_episode(
                    episode["entry"].href,
                    episode["num"],
                    anime_title,
                    download_dir,
                    retry_pass=retry_pass,
                    prior_reason=episode.get("reason"),
                )
                if result.success:
                    total_downloaded_bytes += result.downloaded_bytes
                else:
                    episode["reason"] = result.reason
                    if result.retriable:
                        failures_this_pass.append(episode)
                    else:
                        final_failures.append(episode)
                    print(
                        f"재시도 실패 분류: {episode['num']}화 | reason={result.reason} | retriable={result.retriable}"
                    )
            retriable_failures = failures_this_pass
            if retriable_failures and retry_pass < self.max_retries:
                backoff_sec = min(2 * retry_pass, 10)
                print(f"재시도 대기: {backoff_sec}초 후 다음 패스를 시작합니다.")
                time.sleep(backoff_sec)

        if retriable_failures:
            print(
                "재시도 종료: 최종 실패 회차 -> "
                + ", ".join(f"{episode['num']}화({episode['reason']})" for episode in retriable_failures)
            )
        if self._captcha_detected:
            print("안내: 캡차/봇 확인이 감지되었습니다. 로그인 창에서 캡차를 완료한 뒤 다시 실행해 주세요.")
            self._emit("job_error", reason="captcha_detected")

        unresolved_failures = final_failures + retriable_failures

        result = {
            "anime_id": self.anime_id,
            "anime_title": anime_title,
            "episode_count": len(episode_entries),
            "failed_count": len(unresolved_failures),
            "downloaded_bytes": total_downloaded_bytes,
        }
        self._emit("job_done", **result)
        return self.driver, result
