from pathlib import Path
from unittest import mock

from subman import manager
from subman.library import Media


def _m(path):
    return Media(1, path, "movie", "x", None, None, None, 0, None)


def test_spanish_folder():
    assert manager._native(_m("/media/Películas en Español/Coco (2017).mkv"), "es")
    assert manager._native(_m("/media/Novelas/La reina/E01.mkv"), "es")


def test_audio_tag():
    info = {"streams": [{"codec_type": "audio", "tags": {"language": "spa"}, "disposition": {"default": 1}}]}
    with mock.patch.object(manager.audio, "probe", return_value=info):
        assert manager._native(_m("/media/Peliculas/Coco.mkv"), "es")
    info["streams"][0]["tags"]["language"] = "eng"
    with mock.patch.object(manager.audio, "probe", return_value=info):
        assert manager._native(_m("/media/Series/Friends/S01E01.mkv"), "es") is None
