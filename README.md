# Laftel Downloader

라프텔 작품 ID 기준으로 회차를 수집하고, DRM 키를 추출해서 파일로 저장하는 스크립트입니다.

## 개요
- 로그인 세션은 `./.chrome-profile` 재사용
- 회차 링크 수집 후 MPD/License 요청 감지
- 키 추출 후 `N_m3u8DL-RE`로 다운로드
- 저장 위치: `./downloads/<작품명>/`

## 실행 환경
- Windows 10/11
- Python 3.13.x
- Google Chrome 설치

## 필수 파일/도구
- `license/device.wvd`
- `binaries/N_m3u8DL-RE.exe`
- `binaries/mkvmerge.exe`
- `binaries/mp4decrypt.exe`
- `yt-dlp` (권장: `pip install -r requirements.txt`로 설치)

## 설치
```powershell
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## CLI 사용법
기본 실행:
```powershell
python main.py --anime-id 42947
```

특정 회차만 실행:
```powershell
python main.py --anime-id 42947 --episodes "1-3,5,7"
```

### CLI 인자
| 인자 | 설명 | 예시 |
|---|---|---|
| `--anime-id` | 라프텔 작품 ID | `--anime-id 42947` |
| `--episodes` | 회차 선택 문자열(선택) | `--episodes "1-3,5,7"` |

### `--episodes` 형식
- 단일 회차: `4`
- 범위: `1-6`
- 혼합: `1-3,5,8-10`
- 공백은 자동 정리됨
- 잘못된 형식이면 시작 전에 즉시 오류로 종료됨

## WebUI 사용법
서버 실행:
```powershell
python webui_server.py
```

접속:
- 브라우저에서 `http://127.0.0.1:8000`

순서:
1. `세션 확보` 클릭
2. 로그인 필요 시 `로그인 창`으로 로그인/프로필 선택
3. 작품 ID 입력
4. 필요 시 회차 범위 입력 (`1-3,5,7`)
5. `다운로드 시작` 클릭
6. 하단 로그/상태 확인

## 동작 메모
- 세션 확인이 되면 가능하면 헤드리스로 진행합니다.
- 헤드리스에서 라이선스 감지가 실패하면 창 모드 세션으로 자동 재시도합니다.
- 강제 종료 잔여물은 다음 실행 시 자동 정리 로직이 동작합니다.

## 자주 보는 로그
- `외부 도구 점검 PATH 헤드`: 실행 도구 탐색 시작
- `MPD 요청 감지`: 스트림 메타데이터 요청 확인됨
- `라이선스 요청 감지`: 키 추출 가능한 요청 확인됨
- `headless에서 라이선스 요청 감지에 실패`: 자동 fallback 경고(즉시 재시도 경로)

## 주의
- 개인 학습/연구 용도로만 사용하세요.
- 관련 서비스 약관/저작권 정책을 확인하고 사용하세요.
