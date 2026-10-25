import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.posts import PostStore, parse_posts, video_id


def test_video_id_formats():
    vid = "WHxCRrZ5XLs"
    for url in (f"https://youtu.be/{vid}?si=abc", f"https://www.youtube.com/watch?v={vid}&t=10",
                f"https://youtube.com/live/{vid}?si=x", f"https://m.youtube.com/shorts/{vid}",
                f"https://www.youtube.com/embed/{vid}"):
        assert video_id(url) == vid, url
    for bad in ("javascript:alert(1)", "https://youtube.com.evil.com/watch?v=WHxCRrZ5XLs",
                "https://youtu.be/short", "https://www.notion.so/abc", "https://www.youtube.com/@channel"):
        assert video_id(bad) == "", bad


def test_parse_posts_builds_safe_urls():
    rows = parse_posts({"posts": ["https://youtu.be/WHxCRrZ5XLs?si=x",
                                  {"url": "https://evil.com/x", "title": "x"},
                                  {"url": "https://youtu.be/abcdefghijk", "title": "직접 제목", "date": "2026-10-26"}]})
    assert [r["id"] for r in rows] == ["WHxCRrZ5XLs", "abcdefghijk"]
    assert rows[0]["url"] == "https://www.youtube.com/watch?v=WHxCRrZ5XLs"
    assert rows[0]["thumb"] == "https://i.ytimg.com/vi/WHxCRrZ5XLs/hqdefault.jpg"
    assert rows[1]["title"] == "직접 제목" and rows[1]["date"] == "2026-10-26"


def test_title_fetched_in_background(tmp_path, monkeypatch):
    p = tmp_path / "h.json"
    p.write_text(json.dumps({"posts": [{"url": "https://youtu.be/WHxCRrZ5XLs"}]}), encoding="utf-8")
    store = PostStore(str(p))
    monkeypatch.setattr(store, "_fetch_titles",
                        lambda ids: (setattr(store, "_titles", {i: "자동 제목" for i in ids}),
                                     setattr(store, "_fetching", False)))
    store.all()
    time.sleep(0.2)
    assert store.all()[0]["title"] == "자동 제목"


def test_bundled_file_loads_and_renders():
    from app import create_app
    app = create_app(start_poller=False)
    try:
        posts = app.extensions["posts"].all()
        assert posts and posts[0]["id"] == "WHxCRrZ5XLs"
        html = app.test_client().get("/").get_data(as_text=True)
        assert 'id="viewHaru"' in html and "i.ytimg.com/vi/WHxCRrZ5XLs/hqdefault.jpg" in html
    finally:
        app.extensions["feed"].stop()
        app.extensions["market"].stop()
