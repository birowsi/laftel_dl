# laftel
`by hanbi`

라프텔에서 내가 볼 수 있는 작품을 로컬로 받으려고 만든 개인용 스크립트.
자동 로그인은 안 쓰고, 크롬에서 직접 로그인한 뒤 진행하는 방식.

## 뭐 하는 건가
- 크롬으로 라프텔 페이지 열기
- 로그인/프로필 선택 수동으로 완료
- 에피소드 링크 수집
- MPD + 라이선스 요청 잡아서 키 추출
- `N_m3u8DL-RE`로 다운로드

## 현재 동작 방식
- 로그인: 수동 (`Enter`로 다음 단계 진행)
- 세션 유지: `./.chrome-profile` 재사용
- 저장 경로: `./downloads/<작품명>/`
- 품질: 비디오/오디오 `best`

## 준비물
- Python 3.13.x (현재 프로젝트는 3.13.11 기준으로 확인)
- Chrome 설치
- `license/device.wvd`
- `binaries` 폴더에 아래 파일
- `N_m3u8DL-RE.exe`
- `mkvmerge.exe`
- `mp4decrypt.exe`
- `ffmpeg.exe`

## 설치
```powershell
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 실행
```powershell
python main.py
```

실행하면 브라우저가 뜸.
로그인/프로필 선택하고 터미널로 돌아와서 `Enter` 누르면 진행됨.

## 자주 나는 이슈
- `mp4decrypt not found`
- `binaries/mp4decrypt.exe` 없음. 파일 넣어야 함.

- `[WinError 2] 지정된 파일을 찾을 수 없습니다`
- 보통 외부 바이너리 경로 문제. `binaries` 파일들 확인.

- `총 0개의 에피소드 링크 확보`
- 페이지 로딩이 느리거나 DOM이 바뀐 경우. 다시 실행하거나 선택자 점검.

- `DevToolsActivePort file doesn't exist`
- 같은 프로필로 켜진 크롬 프로세스가 남아있을 때 자주 발생. 크롬 완전히 종료 후 재실행.

## gitignore
민감/대용량/캐시 파일은 커밋 안 하도록 설정됨.
- `/.env`
- `/downloads`
- `/.chrome-profile`

## 주의
개인 학습/백업 용도로만 사용.
콘텐츠 이용 약관, 저작권, 관련 법규는 본인이 책임지고 확인해야 함.
