import os
from tests.conftest import make_test_video, make_test_audio


def test_probe_streams_returns_streams(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'test.mp4')
    make_test_video(path)
    info = prober.probe_streams(path)
    assert 'streams' in info
    assert len(info['streams']) >= 1


def test_has_video_true_for_video(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'vid.mp4')
    make_test_video(path)
    assert prober.has_video(path) is True


def test_has_video_false_for_audio(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'aud.m4a')
    make_test_audio(path)
    assert prober.has_video(path) is False


def test_has_audio_true(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'with.mp4')
    make_test_video(path, with_audio=True)
    assert prober.has_audio(path) is True


def test_has_audio_false(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'without.mp4')
    make_test_video(path, with_audio=False)
    assert prober.has_audio(path) is False


def test_get_duration(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'test.mp4')
    make_test_video(path, duration=5)
    dur = prober.get_duration(path)
    assert 4.5 < dur < 5.5


def test_get_video_params(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'test.mp4')
    make_test_video(path, width=640, height=480)
    w, h, pf = prober.get_video_params(path)
    assert w == 640
    assert h == 480


def test_probe_file(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'test.mp4')
    make_test_video(path, duration=3, width=320, height=240)
    info = prober.probe_file(path)
    assert info['valid'] is True
    assert info['has_video'] is True
    assert info['has_audio'] is True
    assert info['width'] == 320
    assert info['height'] == 240
    assert 2.5 < info['duration'] < 3.5


def test_probe_file_invalid(prober, tmp_dir):
    path = os.path.join(tmp_dir, 'bad.mp4')
    with open(path, 'w') as f:
        f.write('not a video')
    info = prober.probe_file(path)
    assert info['valid'] is False


def test_probe_all_files(prober, tmp_dir):
    p1 = os.path.join(tmp_dir, 'v1.mp4')
    p2 = os.path.join(tmp_dir, 'v2.mp4')
    make_test_video(p1, duration=2)
    make_test_video(p2, duration=3)
    items = [(p1, 0.0), (p2, 5.0)]
    results = prober.probe_all_files(items)
    assert len(results) == 2
    assert results[0]['start_time'] == 0.0
    assert results[1]['start_time'] == 5.0
