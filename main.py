"""
Command Line Interface (CLI) Entrypoint
=======================================

This script provides a terminal-based interface to the Laftel Downloader.
It parses user arguments such as the target anime ID and the optional episode range filter,
then delegates the actual downloading work to `engine.py`.

Usage:
    python main.py --anime-id 16074
    python main.py --anime-id 16074 --episodes "1-3,5"
"""
import argparse

import engine


def parse_args():
    parser = argparse.ArgumentParser(description="laftel CLI downloader")
    parser.add_argument(
        "--anime-id",
        type=int,
        default=None,
        help="다운로드할 작품 ID (입력하지 않으면 실행 후 프롬프트에서 입력)",
    )
    parser.add_argument(
        "--episodes",
        type=str,
        default=None,
        help="다운로드할 회차 범위 (예: 1-3,5,7)",
    )
    return parser.parse_args()


def run_cli(anime_id: int | None, episodes: str | None = None) -> int:
    if anime_id is None:
        try:
            val = input("다운로드할 라프텔 작품 ID를 입력하세요: ").strip()
            anime_id = int(val) if val else engine.DEFAULT_ANIME_ID
        except ValueError:
            print("오류: 올바른 숫자 ID를 입력해야 합니다.")
            return 1
    print(engine.ASCII_ART)
    print(f"대상 작품 ID: {anime_id}")
    print("초기 점검을 시작합니다...")

    try:
        engine.validate_episode_selection(episodes)
    except ValueError as e:
        print(f"오류: 회차 입력 형식이 잘못되었습니다: {e}")
        return 1

    engine.cleanup_stale_download_artifacts()
    if not engine.check_external_tools():
        print("필수 도구 점검에 실패했습니다. 위 로그를 확인한 뒤 다시 실행해 주세요.")
        return 1

    driver = engine.get_or_login_headless_driver(anime_id=anime_id)
    if not driver:
        print("로그인 세션을 확보하지 못해 종료합니다.")
        return 1

    exit_code = 0
    result = None
    try:
        driver, result = engine.run_download_for_anime(driver, anime_id, episode_selection=episodes)
    except KeyboardInterrupt:
        print("사용자 중단 감지: 브라우저 정리 후 종료합니다.")
        exit_code = 130
    except Exception as e:
        print(f"오류: 다운로드 실행 중: {type(e).__name__}: {e}")
        exit_code = 1
    finally:
        engine.safe_quit_driver(driver)

    if result:
        print("\n모든 작업 완료. 브라우저를 종료합니다")
        if result["downloaded_bytes"] > 0:
            total_gb = result["downloaded_bytes"] / (1024 ** 3)
            print(f"총 다운로드 용량: {total_gb:.2f} GB")
        else:
            print("다운로드된 파일이 없습니다.")

    return exit_code


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(run_cli(args.anime_id, args.episodes))
