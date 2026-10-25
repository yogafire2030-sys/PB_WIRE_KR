import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.posts import parse_posts


def test_only_notion_https_urls_accepted():
    rows = parse_posts({"posts": [
        {"title": "ok", "url": "https://app.notion.com/p/abc"},
        {"title": "site", "url": "https://pbnote.notion.site/abc", "date": "2026-10-25"},
        {"title": "xss", "url": "javascript:alert(1)"},
        {"title": "evil", "url": "https://notion.so.evil.com/x"},
        {"title": "", "url": "https://www.notion.so/x"},
        {"title": "bad date", "url": "https://www.notion.so/x", "date": "어제"},
    ]})
    assert [r["title"] for r in rows] == ["ok", "site", "bad date"]
    assert rows[1]["date"] == "2026-10-25" and rows[2]["date"] == ""


def test_bundled_file_loads_and_renders():
    from app import create_app
    app = create_app(start_poller=False)
    try:
        posts = app.extensions["posts"].all()
        assert len(posts) >= 2
        html = app.test_client().get("/").get_data(as_text=True)
        assert 'id="viewHaru"' in html and posts[0]["url"] in html
    finally:
        app.extensions["feed"].stop()
        app.extensions["market"].stop()
