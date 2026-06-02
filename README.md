# Laftel DL
![Version](https://img.shields.io/badge/version-v1.3.1-blue.svg)
![Python](https://img.shields.io/badge/python-3.13-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6.svg)
![WebUI](https://img.shields.io/badge/WebUI-FastAPI-009688.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

다운로더입니다. WebUI와 CLI 환경을 지원합니다.

## Requirements

- Windows 10/11
- Python 3.13
- Google Chrome
- `binaries/device.wvd` (본인의 Widevine L3 CDM 키 필요)
- `binaries/` 폴더 내 필수 바이너리:
  - `N_m3u8DL-RE.exe`
  - `mkvmerge.exe`
  - `mp4decrypt.exe`
  - `7z.exe` (선택 사항, 압축용)

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

**WebUI (권장)**
```powershell
run_webui.bat
```

**CLI**
```powershell
run_cli.bat
run_cli.bat --anime-id 16074
run_cli.bat --anime-id 16074 --episodes "1-3,5"
```

## Disclaimer

개인 연구용 도구입니다. 
발생하는 모든 책임은 사용자에게 있습니다.
