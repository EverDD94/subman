from pathlib import Path

from subman import youtube


def _yt(tmp_path, info):
    y = youtube.YouTube(Path(tmp_path))
    y.info = lambda vid: info
    y._start_mux = lambda *a: None
    return y


def test_prefers_hls_up_to_1080(tmp_path):
    fmts = [
        {"protocol": "m3u8_native", "vcodec": "avc1.4d", "acodec": "mp4a", "height": 720, "url": "a"},
        {"protocol": "m3u8_native", "vcodec": "avc1.64", "acodec": "mp4a", "height": 1080, "url": "b"},
        {"protocol": "m3u8_native", "vcodec": "vp09", "acodec": "mp4a", "height": 2160, "url": "c"},
    ]
    r = _yt(tmp_path, {"id": "dQw4w9WgXcQ", "title": "t", "formats": fmts}).play("dQw4w9WgXcQ")
    assert r["url"] == "b" and r["format"] == "hls"


def test_joins_on_nas_without_hls(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    fmts = [
        {"protocol": "https", "vcodec": "avc1", "acodec": "none", "height": 1080, "url": "v"},
        {"protocol": "https", "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "abr": 128, "url": "a"},
    ]
    y = _yt(tmp_path, {"id": "dQw4w9WgXcQ", "formats": fmts})
    r = y.play("dQw4w9WgXcQ")
    assert r["url"] == f"/yt/{y.sign('dQw4w9WgXcQ')}/dQw4w9WgXcQ/index.m3u8" and r["nas"]


def test_rejects_bad_ids(tmp_path):
    y = youtube.YouTube(Path(tmp_path))
    for bad in ("../../etc/pa", "short", "a" * 12):
        try:
            y.mux_file(bad, "index.m3u8")
            assert False
        except ValueError:
            pass
