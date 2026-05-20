import os
import subprocess
from mtslinker.grid import GridCompositor, GridLayout, PresenterLayout, _even, _build_audio_filter
from mtslinker.timeline import GridSource
from tests.conftest import make_test_video


# --- GridLayout unit tests ---

def test_compute_layout_single(ffmpeg):
    assert GridLayout.compute_layout(1) == (1, 1)


def test_compute_layout_two(ffmpeg):
    assert GridLayout.compute_layout(2) == (2, 1)


def test_compute_layout_four(ffmpeg):
    assert GridLayout.compute_layout(4) == (2, 2)


def test_compute_layout_five(ffmpeg):
    assert GridLayout.compute_layout(5) == (3, 2)


def test_even():
    assert _even(640) == 640
    assert _even(641) == 640
    assert _even(1) == 0


def test_grid_composite_two_webcams(ffmpeg, tmp_dir):
    grid = GridLayout(ffmpeg)
    v1 = os.path.join(tmp_dir, 'v1.mp4')
    v2 = os.path.join(tmp_dir, 'v2.mp4')
    out = os.path.join(tmp_dir, 'grid.mp4')
    make_test_video(v1, duration=2)
    make_test_video(v2, duration=2)
    sources = [GridSource(v1, 0), GridSource(v2, 0)]
    grid.composite(sources, 2.0, out, 640, 360)
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0


def test_grid_output_matches_target_resolution(ffmpeg, prober, tmp_dir):
    """Grid output must be exactly target_w x target_h for concat safety."""
    grid = GridLayout(ffmpeg)
    v1 = os.path.join(tmp_dir, 'v1.mp4')
    v2 = os.path.join(tmp_dir, 'v2.mp4')
    v3 = os.path.join(tmp_dir, 'v3.mp4')
    out = os.path.join(tmp_dir, 'grid.mp4')
    make_test_video(v1, duration=2, width=320, height=240)
    make_test_video(v2, duration=2, width=640, height=480)
    make_test_video(v3, duration=2, width=320, height=240)
    sources = [GridSource(v1, 0), GridSource(v2, 0), GridSource(v3, 0)]
    grid.composite(sources, 2.0, out, 1280, 720)
    info = prober.probe_file(out)
    assert info['width'] == 1280
    assert info['height'] == 720


# --- PresenterLayout unit tests ---

def test_presenter_main_only(ffmpeg, prober, tmp_dir):
    """Presenter with only main source, no overlay."""
    presenter = PresenterLayout(ffmpeg)
    v = os.path.join(tmp_dir, 'main.mp4')
    out = os.path.join(tmp_dir, 'pres.mp4')
    make_test_video(v, duration=3, with_audio=True)
    main = GridSource(v, 0, has_audio=True)
    presenter.composite(main, None, [], 3.0, out, 640, 360)
    assert os.path.exists(out)
    info = prober.probe_file(out)
    assert info['width'] == 640
    assert info['height'] == 360
    assert info['has_audio']


def test_presenter_with_pip(ffmpeg, prober, tmp_dir):
    """Presenter with main + PIP overlay."""
    presenter = PresenterLayout(ffmpeg)
    v_main = os.path.join(tmp_dir, 'main.mp4')
    v_pip = os.path.join(tmp_dir, 'pip.mp4')
    out = os.path.join(tmp_dir, 'pres.mp4')
    make_test_video(v_main, duration=3, with_audio=True)
    make_test_video(v_pip, duration=3, with_audio=True)
    main = GridSource(v_main, 0, has_audio=True, is_admin=True)
    overlay = GridSource(v_pip, 0, has_audio=True)
    presenter.composite(main, overlay, [], 3.0, out, 640, 360)
    assert os.path.exists(out)
    info = prober.probe_file(out)
    assert info['width'] == 640
    assert info['height'] == 360
    assert info['has_audio']


def test_presenter_with_extra_audio(ffmpeg, prober, tmp_dir):
    """Presenter with main + overlay + extra audio-only source."""
    presenter = PresenterLayout(ffmpeg)
    v_main = os.path.join(tmp_dir, 'main.mp4')
    v_pip = os.path.join(tmp_dir, 'pip.mp4')
    v_extra = os.path.join(tmp_dir, 'extra.mp4')
    out = os.path.join(tmp_dir, 'pres.mp4')
    make_test_video(v_main, duration=3, with_audio=True)
    make_test_video(v_pip, duration=3, with_audio=False)
    make_test_video(v_extra, duration=3, with_audio=True)
    main = GridSource(v_main, 0, has_audio=True, is_admin=True)
    overlay = GridSource(v_pip, 0, has_audio=False)
    extra = [GridSource(v_extra, 0, has_audio=True)]
    presenter.composite(main, overlay, extra, 3.0, out, 640, 360)
    assert os.path.exists(out)
    info = prober.probe_file(out)
    assert info['has_audio']


def test_presenter_resolution_matches_target(ffmpeg, prober, tmp_dir):
    """Presenter output must be exactly target_w x target_h."""
    presenter = PresenterLayout(ffmpeg)
    # Source is 320x240 but target is 1280x720
    v_main = os.path.join(tmp_dir, 'main.mp4')
    v_pip = os.path.join(tmp_dir, 'pip.mp4')
    out = os.path.join(tmp_dir, 'pres.mp4')
    make_test_video(v_main, duration=2, width=320, height=240, with_audio=True)
    make_test_video(v_pip, duration=2, width=160, height=120, with_audio=True)
    main = GridSource(v_main, 0, has_audio=True)
    overlay = GridSource(v_pip, 0, has_audio=True)
    presenter.composite(main, overlay, [], 2.0, out, 1280, 720)
    info = prober.probe_file(out)
    assert info['width'] == 1280
    assert info['height'] == 720


# --- _build_audio_filter unit tests ---

def test_build_audio_filter_no_audio():
    sources = [GridSource('a.mp4', 0, has_audio=False)]
    filt, audio_map = _build_audio_filter(sources, 5.0)
    assert filt == ''
    assert audio_map == []


def test_build_audio_filter_single():
    sources = [GridSource('a.mp4', 0, has_audio=True)]
    filt, audio_map = _build_audio_filter(sources, 5.0)
    assert '[aout]' in filt
    assert audio_map == ['-map', '[aout]']


def test_build_audio_filter_multi():
    sources = [
        GridSource('a.mp4', 0, has_audio=True),
        GridSource('b.mp4', 0, has_audio=True),
    ]
    filt, audio_map = _build_audio_filter(sources, 5.0)
    assert 'amix=inputs=2' in filt
    assert audio_map == ['-map', '[aout]']


def test_build_audio_filter_mixed():
    sources = [
        GridSource('a.mp4', 0, has_audio=True),
        GridSource('b.mp4', 0, has_audio=False),
        GridSource('c.mp4', 0, has_audio=True),
    ]
    filt, audio_map = _build_audio_filter(sources, 5.0)
    assert 'amix=inputs=2' in filt
    assert '[0:a]' in filt
    assert '[2:a]' in filt
    assert '[1:a]' not in filt


# --- GridCompositor facade backward compat ---

def test_facade_composite(ffmpeg, tmp_dir):
    """GridCompositor facade delegates to GridLayout."""
    comp = GridCompositor(ffmpeg)
    v1 = os.path.join(tmp_dir, 'v1.mp4')
    v2 = os.path.join(tmp_dir, 'v2.mp4')
    out = os.path.join(tmp_dir, 'grid.mp4')
    make_test_video(v1, duration=2)
    make_test_video(v2, duration=2)
    # Legacy tuple interface
    comp.composite([(v1, 0), (v2, 0)], 2.0, out, 640, 360)
    assert os.path.exists(out)


def test_facade_compute_layout():
    assert GridCompositor.compute_layout(4) == (2, 2)


def test_facade_even():
    assert GridCompositor.even(641) == 640
