# Laftel Widevine L3 DRM Bypass Anime Downloader

라프텔애니일괄다운스크립트  

## 뭣
- 크롬으로 라프텔 페이지 열기
- 로그인/프로필 선택 수동으로 완료
- 에피소드 링크 수집
- MPD + 라이선스 요청 잡아서 키 추출
- `N_m3u8DL-RE`로 다운로드

## 헉
- 로그인: 수동 (`Enter`로 다음 단계 진행)
- 세션 유지: `./.chrome-profile` 재사용
- 저장 경로: `./downloads/<작품명>/`
- 품질: 비디오/오디오 `best`

## 엥
- Windows 10 64 Bit or Windows 11
- Python 3.13.x (3.13.11 기준 확인)
- Chrome 설치

## 엇
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

## 주의
개인 학습/연구 용도로만 사용.  
`by hanbi.`