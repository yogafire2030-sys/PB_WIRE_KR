@echo off
chcp 65001 > nul
title [PB수첩 글로벌 속보 (made by 박*남)] 원클릭 실행
echo =========================================================
echo   [PB수첩 글로벌 속보 (made by 박*남)] 서버 및 외부 공유 터널 실행
echo =========================================================
echo.
echo  1. 실시간 속보 수집/번역 서버를 시작합니다...
start /b python run.py
timeout /t 3 > nul
echo.
echo  2. 외부망(LTE/5G/스마트폰/카카오톡) 전용 HTTPS 공유 링크를 연결합니다...
echo.
echo  [공유 방법]
echo  잠시 후 아래 로그에 표시되는
echo  'https://...trycloudflare.com' 주소를 복사하여
echo  카카오톡으로 공유하시면 외부망에 있는 누구나 열어볼 수 있습니다!
echo =========================================================
echo.
cloudflared.exe tunnel --url http://127.0.0.1:5000
pause
