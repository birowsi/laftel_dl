# FILE: smoke_test.py
# AI_NOTE: Lightweight local smoke test for module wiring. Does not open browser or perform network/download actions.
import inspect

import browser_session
import download_job
import drm_support
import engine
import runtime_support
import webui_server


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def run_smoke():
    _assert(engine.DEFAULT_ANIME_ID > 0, "DEFAULT_ANIME_ID must be positive")
    _assert(hasattr(engine, "run_download_for_anime"), "engine facade missing run_download_for_anime")
    _assert(hasattr(engine, "get_or_login_headless_driver"), "engine facade missing session API")
    _assert(hasattr(runtime_support, "check_external_tools"), "runtime_support missing tool check")
    _assert(hasattr(drm_support, "get_key_original"), "drm_support missing key extractor")
    _assert(hasattr(browser_session, "ensure_logged_in"), "browser_session missing ensure_logged_in")
    _assert(hasattr(download_job, "DownloadJob"), "download_job missing DownloadJob class")
    parsed = download_job.DownloadJob.parse_episode_selection("1-3,5,7")
    _assert(parsed == {1, 2, 3, 5, 7}, "episode selection parser mismatch")
    _assert(hasattr(webui_server, "app"), "webui_server missing FastAPI app")

    sig = inspect.signature(engine.run_download_for_anime)
    _assert("on_event" in sig.parameters, "run_download_for_anime should support on_event hook")
    _assert("episode_selection" in sig.parameters, "run_download_for_anime should support episode_selection")

    print("smoke ok")


if __name__ == "__main__":
    run_smoke()
