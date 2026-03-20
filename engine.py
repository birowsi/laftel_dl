# FILE: engine.py
# AI_NOTE: Compatibility facade. Re-exports session/runtime APIs and runs DownloadJob with optional event hook and episode-range support.
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

ASCII_ART = r"""
    __                        
   / /_  __  __               
  / __ \/ / / /               
 / /_/ / /_/ /                
/_.___/\__, /         __    _ 
   / //____/_ _____  / /_  (_)
  / __ \/ __ `/ __ \/ __ \/ / 
 / / / / /_/ / / / / /_/ / /  
/_/ /_/\__,_/_/ /_/_.___/_/   
"""

DEFAULT_ANIME_ID = 40846


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
