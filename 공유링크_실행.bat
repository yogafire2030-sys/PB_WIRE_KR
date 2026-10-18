@echo off
chcp 65001 > nul
echo =====================================================
echo  [PB수첩 글로벌 속보 (made by 박*남)] 카톡 공유 링크 실행
echo =====================================================
echo.
echo  로컬 서버(http://127.0.0.1:5000)를 외부 공유 터널로 연결합니다.
echo  잠시 후 화면에 표시되는 https://...trycloudflare.com 링크를 복사해 공유하세요.
echo.
cloudflared.exe tunnel --url http://127.0.0.1:5000
pause
