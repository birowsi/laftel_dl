# Laftel Downloader 🚀

![Version](https://img.shields.io/badge/version-v1.3.1-blue.svg)
![Python](https://img.shields.io/badge/python-3.13-blue.svg)
![License](https://img.shields.io/badge/license-Private-red.svg)

라프텔(Laftel) 작품 ID를 기준으로 회차를 수집하고, DRM 네트워크 흐름을 캡처하여 스트리밍 영상을 다운로드하는 자동화 도구입니다. **직관적이고 아름다운 다크 모드 WebUI**와 **CLI**를 모두 지원합니다.

> ⚠️ **주의**: 본 레포지토리는 개인의 학습 및 연구 목적을 위해 작성되었습니다. 서비스 약관(DRM 우회, 자동화 등)에 위배될 수 있으며, 모든 법적 책임은 사용자 본인에게 있습니다.

---

## ✨ 핵심 기능 (Features)
- 🎨 **세련된 WebUI**: 다크 모드, Glassmorphism 디자인, 실시간 로그 및 상태 표시.
- 🔑 **세션 유지**: 크롬 프로필(`/ .chrome-profile`)을 저장하여 반복적인 로그인 최소화.
- 🎯 **선택적 다운로드**: 1-3,5,7 등 원하는 회차만 골라서 다운로드 가능.
- 🔄 **자동 재시도 로직**: 간헐적인 네트워크 에러나 브라우저 끊김 시 자동 복구 및 재시도.
- 📦 **WebUI 내장 분할압축**: 다운로드가 완료된 폴더를 버튼 클릭 한 번에 500MB 단위로 7z 분할 압축.

---

## 🛠️ 설치 및 준비 (Prerequisites)

이 프로젝트는 Widevine L3 CDM 키가 필요합니다. 레포지토리에 기본 키가 포함되어 있지 않으므로, 사용 전 **반드시 본인의 키를 준비**해야 합니다.

### 1. 요구 사항
- **OS**: Windows 10 / 11
- **Python**: 3.13.x
- **Google Chrome**: 최신 버전 설치 필수
- **외부 바이너리 (필수)**: `binaries/` 폴더 내에 다음 파일이 존재해야 합니다.
  - `N_m3u8DL-RE.exe`
  - `mkvmerge.exe`
  - `mp4decrypt.exe`
  - `7z.exe` 또는 `7za.exe` (분할압축 기능 사용 시)
- **Widevine CDM 키 (가장 중요)**:
  - `license/device.wvd` 파일이 반드시 존재해야 합니다.
  - 이 파일은 본인의 기기에서 추출하여 `license/` 디렉터리 안에 직접 넣어주세요.

### 2. 패키지 설치
```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 🚀 사용 방법 (Usage)

### 💻 1. WebUI (추천)
브라우저 환경에서 모든 것을 마우스 클릭으로 제어할 수 있습니다.
```powershell
run_webui.bat
```
1. 스크립트 실행 시 브라우저가 자동 오픈됩니다. (`http://127.0.0.1:8000`)
2. **[세션 확보]** 버튼을 클릭하여 로그인을 진행합니다.
3. 로그인 및 프로필 선택이 끝나면 **[다운로드 시작]** 버튼이 활성화됩니다.
4. 애니메이션 ID (예: `16074`)를 입력하고 다운로드를 시작하세요.

*(팁: 여러 작품을 한 번에 받으려면 ID를 콤마(,)로 구분하세요. 예: `16074,42947`)*

### ⌨️ 2. CLI
터미널 환경에서 백그라운드로 작동시키고 싶을 때 사용합니다.
```powershell
# 전체 회차 다운로드
run_cli.bat --anime-id 16074

# 특정 회차만 지정해서 다운로드
run_cli.bat --anime-id 16074 --episodes "1-3,5,7"
```

---

## 📁 디렉터리 구조 (Structure)

- `/binaries`: 외부 필수 실행 파일들이 위치하는 폴더
- `/license`: **(사용자 직접 추가)** `device.wvd` 와이드바인 키 폴더
- `/downloads`: 다운로드 완료된 원본 영상 폴더
- `/archives`: WebUI에서 분할압축 시 결과물이 저장되는 폴더
- `webui_server.py` / `webui_index.html`: WebUI 백엔드(FastAPI) 및 프론트엔드
- `engine.py` / `download_job.py`: 코어 크롤링 및 다운로드 엔진 로직
- `browser_session.py`: 크롬 세션 관리 및 우회 로직

---

## 🐛 트러블슈팅 (Troubleshooting)

- **Q. "로그인이 필요합니다"가 무한 반복돼요.**
  - A. 캡차(CAPTCHA)에 걸렸을 확률이 높습니다. WebUI에서 `세션 확보`를 누른 뒤 뜨는 창에서 캡차를 수동으로 풀어주고 프로필을 선택해 주세요.
- **Q. 압축 퍼센트가 안 올라가거나 오류가 나요.**
  - A. 시스템 환경 변수(PATH)나 `binaries/` 폴더 내에 `7z.exe` 혹은 `7za.exe`가 정상적으로 위치하는지 확인해 주세요.
- **Q. 백그라운드 이미지를 바꾸고 싶어요.**
  - A. `background.png` (대소문자 일치) 파일을 레포지토리 루트에 넣고 실행하면 다크 그라데이션 대신 커스텀 이미지가 적용됩니다.

---

## 📝 라이선스 및 고지사항
본 프로그램의 사용으로 인해 발생하는 계정 정지, 법적 분쟁 등에 대해 개발자는 어떠한 책임도 지지 않습니다. 
**반드시 개인 소장 및 연구용**으로만 사용하시기 바랍니다.
