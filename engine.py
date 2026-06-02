"""
High-Level Facade Engine
========================

This module serves as the primary gateway for both the CLI (`main.py`) and the WebUI (`webui_server.py`).
It abstracts away the complexities of the underlying modules.

Key Responsibilities:
1. Re-exporting critical session and runtime APIs.
2. Initializing the `DownloadJob` and injecting any required event hooks.
3. Handling high-level error capture and cleanup logic for downloaded artifacts.
"""
from browser_session import (
    create_webdriver_with_profile,
    ensure_logged_in,
    get_headless_driver_if_session_exists,
    get_or_login_headless_driver,
    has_authenticated_player_access,
    is_home_url,
    is_login_url,
    is_target_player_link,
    login_and_select_profile_wire,
    recreate_driver_headless,
)
from download_job import DownloadJob
from runtime_support import (
    LOGGER,
    check_external_tools,
    cleanup_stale_download_artifacts,
    safe_quit_driver,
)


DEFAULT_ANIME_ID = 16074


def run_download_for_anime(
    driver,
    anime_id,
    max_retries=5,
    should_stop=None,
    on_event=None,
    episode_selection=None,
):
    job = DownloadJob(
        driver=driver,
        anime_id=anime_id,
        max_retries=max_retries,
        should_stop=should_stop,
        on_event=on_event,
        episode_selection=episode_selection,
    )
    return job.run()


def validate_episode_selection(selection_text: str | None):
    return DownloadJob.parse_episode_selection(selection_text)
