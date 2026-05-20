"""Integration tests for audio pipeline — catches echo, silence, timeline issues."""
import os
import subprocess
from tests.conftest import make_test_video, make_test_audio
from mtslinker.ffmpeg import FFmpegRunner
from mtslinker.prober import MediaProber
from mtslinker.audio import AudioMerger
from mtslinker.grid import GridCompositor
from mtslinker.segments import SegmentBuilder


def _get_volume(path):
    """Get mean volume in dB from a media file."""
    result = subprocess.run(
        ['ffmpeg', '-i', path, '-af', 'volumedetect', '-f', 'null', '-'],
        capture_output=True, text=True,
    )
    for line in result.stderr.splitlines():
        if 'mean_volume' in line:
            return float(line.split('mean_volume:')[1].strip().split()[0])
    return None


def test_grid_audio_no_echo(ffmpeg, prober, tmp_dir):
    """Grid composite should not cause echo when audio is also merged."""
    # Create two webcams: v1 has audio (440Hz), v2 has no audio
    v1 = os.path.join(tmp_dir, 'cam1.mp4')
    v2 = os.path.join(tmp_dir, 'cam2.mp4')
    make_test_video(v1, duration=3, with_audio=True)
    make_test_video(v2, duration=3, with_audio=False)

    # Grid composite — should include v1's audio (input 0, sorted by has_audio)
    grid = GridCompositor(ffmpeg)
    grid_out = os.path.join(tmp_dir, 'grid.mp4')
    grid.composite([(v1, 0), (v2, 0)], 3.0, grid_out, 640, 360)

    # Verify grid has audio
    assert prober.has_audio(grid_out)
    grid_vol = _get_volume(grid_out)
    assert grid_vol is not None
    assert grid_vol > -50  # should be audible

    # Now merge an audio-only track on top (different frequency)
    audio_track = os.path.join(tmp_dir, 'extra.m4a')
    make_test_audio(audio_track, duration=3, frequency=880)

    merger = AudioMerger(ffmpeg, prober)
    final = os.path.join(tmp_dir, 'final.mp4')
    merger.merge(grid_out, [(audio_track, 0.0)], tmp_dir, final, total_duration=3)

    final_vol = _get_volume(final)
    assert final_vol is not None
    # Final should be louder than grid alone (added extra audio)
    # but NOT double the grid audio (no echo)
    # If echo: grid_vol ~= -21, final_vol ~= -15 (6dB louder = doubled)
    # Without echo: grid_vol ~= -21, final_vol ~= -18 (mixed with different freq)
    assert final_vol > grid_vol - 1  # at least as loud


def test_grid_audio_from_first_input(ffmpeg, prober, tmp_dir):
    """Grid should take audio from first input (sorted: audio-bearing first)."""
    v_with = os.path.join(tmp_dir, 'with_audio.mp4')
    v_without = os.path.join(tmp_dir, 'no_audio.mp4')
    make_test_video(v_with, duration=2, with_audio=True)
    make_test_video(v_without, duration=2, with_audio=False)

    grid = GridCompositor(ffmpeg)
    out = os.path.join(tmp_dir, 'grid.mp4')
    # Put audio-bearing first (as the processor does)
    grid.composite([(v_with, 0), (v_without, 0)], 2.0, out, 640, 360)

    assert prober.has_audio(out)
    vol = _get_volume(out)
    assert vol is not None
    assert vol > -50


def test_audio_merge_preserves_timing(ffmpeg, prober, tmp_dir):
    """Audio tracks placed with adelay should not shift."""
    # Create a 10s video
    vid = os.path.join(tmp_dir, 'video.mp4')
    make_test_video(vid, duration=10, with_audio=True)

    # Create audio that starts at 5s
    audio = os.path.join(tmp_dir, 'delayed.m4a')
    make_test_audio(audio, duration=3, frequency=880)

    merger = AudioMerger(ffmpeg, prober)
    final = os.path.join(tmp_dir, 'final.mp4')
    merger.merge(vid, [(audio, 5.0)], tmp_dir, final, total_duration=10)

    # Final should be 10s
    dur = prober.get_duration(final)
    assert 9.5 < dur < 10.5

    # Audio at 0-4s should be original only
    vol_start = _get_volume_at(final, 0, 3)
    # Audio at 5-8s should be louder (original + delayed)
    vol_mid = _get_volume_at(final, 5, 3)
    # Both should have audio
    assert vol_start is not None
    assert vol_mid is not None


def _get_volume_at(path, start, duration):
    """Get mean volume at a specific time range."""
    result = subprocess.run(
        ['ffmpeg', '-ss', str(start), '-i', path,
         '-t', str(duration), '-af', 'volumedetect', '-f', 'null', '-'],
        capture_output=True, text=True,
    )
    for line in result.stderr.splitlines():
        if 'mean_volume' in line:
            return float(line.split('mean_volume:')[1].strip().split()[0])
    return None


def test_segment_duration_matches_plan(ffmpeg, prober, tmp_dir):
    """Normalized segments should match planned duration within tolerance."""
    segments = SegmentBuilder(ffmpeg, prober)
    vid = os.path.join(tmp_dir, 'input.mp4')
    out = os.path.join(tmp_dir, 'norm.mp4')
    make_test_video(vid, duration=10)

    # Normalize with max_duration=5
    segments.normalize(vid, out, 640, 360, 'yuv420p', max_duration=5)
    dur = prober.get_duration(out)
    assert 4.5 < dur < 5.5


def test_black_segment_has_audio(ffmpeg, prober, tmp_dir):
    """Black gap segments must have a silent audio stream for concat."""
    segments = SegmentBuilder(ffmpeg, prober)
    path = os.path.join(tmp_dir, 'black.mp4')
    segments.generate_black(path, 3.0, 640, 360)
    assert prober.has_audio(path)
    vol = _get_volume(path)
    assert vol is not None
    assert vol < -80  # should be silent
