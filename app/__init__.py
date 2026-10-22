"""Flask 애플리케이션 팩토리."""
import atexit
import logging
import os

from flask import Flask

from config import Config
from .services.feed import FeedService
from .services.market import MarketService


def create_app(config_object=Config, start_poller: bool = True) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    feed = FeedService(app.config)
    app.extensions["feed"] = feed
    market = MarketService(app.config)
    app.extensions["market"] = market

    from .routes import bp
    app.register_blueprint(bp)

    @app.after_request
    def _security_headers(resp):
        h = resp.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "SAMEORIGIN"
        h["Referrer-Policy"] = "no-referrer-when-downgrade"
        h["Content-Security-Policy"] = (
            "default-src 'self' data: https: 'unsafe-inline' 'unsafe-eval'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' https://cdn.jsdelivr.net data:; "
            "img-src 'self' data: https:; "
            "connect-src 'self' *; "
        )
        if resp.mimetype == "application/json":
            h["Cache-Control"] = "no-store"
        return resp

    # 디버그 리로더 사용 시 부모 프로세스에서는 수집 스레드를 띄우지 않음(중복 방지)
    if start_poller and (not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        feed.start()
        market.start()
        atexit.register(feed.stop)
        atexit.register(market.stop)

    return app
