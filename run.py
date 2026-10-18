"""실행 진입점.

개발/단독 실행 : python run.py
운영(권장)     : waitress-serve --host=0.0.0.0 --port=5000 --threads=8 run:app
※ 수집 스레드가 프로세스마다 생기므로 멀티 프로세스(워커 여러 개)는 쓰지 마세요.
"""
import os

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402  (환경변수 로드 후 import)

app = create_app()

import socket

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    local_ip = get_local_ip()
    print(f" * 로컬 접속 주소: http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f" * 다른 사람 공유 링크 (동일 Wi-Fi/사내망): http://{local_ip}:{port}")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)
