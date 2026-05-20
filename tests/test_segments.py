import os
import subprocess

import pytest

from tests.conftest import make_test_video
from mtslinker.segments import SegmentBuilder


class _SpyFFmpeg:
    """Captures the last ffmpeg command instead of running it."""

    def __init__(self):
        self.cmd = None

    def run(self, cmd, description='ffmpeg'):
        self.cmd = cmd

    def get_video_encoder_fast(self):
        return ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23']


class _FakeProber:
    """No audio in input, fixed duration — exercises the silent-audio path."""

    def __init__(self, duration):
        self._duration = duration

    def probe_streams(self, path):
        return {'streams': [{'codec_type': 'video'}]}

    def get_duration(self, path):
        return self._duration


def _flag_index(cmd, flag):
    return cmd.index(flag)


def test_ensure_audio_guards_against_interleave_oom():
    """The silent-audio mux must not OOM via the muxer interleave buffer.

    Regression guard for av_interleaved_write_frame "Cannot allocate
    memory": with -c:v copy + a lazily-generated anullsrc, ffmpeg buffers
    the whole copied video in RAM unless we (a) cap the interleave buffer
    and (b) make the silent input finite. A small test clip never triggers
    the real OOM, so we assert the *command* keeps both safeguards.
    """
    spy = _SpyFFmpeg()
    builder = SegmentBuilder(spy, _FakeProber(duration=1234.0))
    builder.ensure_audio('in.mp4', 'out.mp4')
    cmd = spy.cmd

    # (a) muxer must not buffer to interleave
    assert '-max_interleave_delta' in cmd
    assert cmd[cmd.index('-max_interleave_delta') + 1] == '0'

    # (b) anullsrc must be a finite input: -t before its -i, not after
    anull = cmd.index('anullsrc=r=44100:cl=stereo')
    t_idx = cmd.index('-t')
    assert t_idx < anull, '-t must bound the anullsrc *input*, not the output'
    assert cmd[t_idx + 1] == '1234.0'

    # correctness not regressed: video still stream-copied
    assert cmd[cmd.index('-c:v') + 1] == 'copy'


def test_ensure_audio_rejects_zero_duration_input():
    """Empty/corrupt input (duration <= 0) must fail loudly, not run ffmpeg.

    Root cause of the production OOM: normalize() seeked past end-of-source
    and produced a 262-byte, duration-0 file; feeding it to ensure_audio ran
    an unbounded anullsrc + -c:v copy mux that buffered silent audio forever
    until "av_interleaved_write_frame: Cannot allocate memory". ensure_audio
    must refuse a non-positive-duration input (raising the same
    CalledProcessError the callers' black-gap fallback catches) and must
    never invoke ffmpeg in that case.
    """
    spy = _SpyFFmpeg()
    builder = SegmentBuilder(spy, _FakeProber(duration=0.0))
    with pytest.raises(subprocess.CalledProcessError):
        builder.ensure_audio('in.mp4', 'out.mp4')
    assert spy.cmd is None, 'ffmpeg must not run on a zero-duration input'


def test_normalize_fails_loudly_on_empty_output():
    """Seek past end-of-source: ffmpeg exits 0 with an empty container.

    normalize() must detect the zero-duration output and raise
    CalledProcessError so the processor falls back to a black gap instead
    of handing an empty file to ensure_audio (the OOM trigger).
    """
    spy = _SpyFFmpeg()
    builder = SegmentBuilder(spy, _FakeProber(duration=0.0))
    with pytest.raises(subprocess.CalledProcessError):
        builder.normalize('in.mp4', 'out.mp4', 640, 360, 'yuv420p',
                          max_duration=90.0, seek=104.0)
    # ffmpeg *was* attempted (the emptiness is only knowable post-run)...
    assert spy.cmd is not None
    # ...and it really was the normalize seek that ran.
    assert spy.cmd[spy.cmd.index('-ss') + 1] == '104.0'


def test_normalize_succeeds_when_output_nonempty():
    """A normal (non-empty) normalize must still return the output path."""
    spy = _SpyFFmpeg()
    builder = SegmentBuilder(spy, _FakeProber(duration=90.0))
    result = builder.normalize('in.mp4', 'out.mp4', 640, 360, 'yuv420p',
                               max_duration=90.0)
    assert result == 'out.mp4'


def test_generate_black(segments, tmp_dir):
    path = os.path.join(tmp_dir, 'black.mp4')
    segments.generate_black(path, 2.0, 320, 240)
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_ensure_audio_keeps_existing(segments, tmp_dir):
    vid = os.path.join(tmp_dir, 'with.mp4')
    out = os.path.join(tmp_dir, 'out.mp4')
    make_test_video(vid, with_audio=True)
    result = segments.ensure_audio(vid, out)
    assert result == vid


def test_ensure_audio_adds_silent(segments, tmp_dir):
    from mtslinker.prober import MediaProber
    vid = os.path.join(tmp_dir, 'without.mp4')
    out = os.path.join(tmp_dir, 'out.mp4')
    make_test_video(vid, with_audio=False, duration=5)
    result = segments.ensure_audio(vid, out)
    assert result == out
    assert os.path.exists(out)
    prober = MediaProber()
    # Silent audio stream must actually be present...
    assert prober.has_audio(out)
    # ...and the -t bound must not truncate the video.
    assert abs(prober.get_duration(out) - prober.get_duration(vid)) < 0.5


def test_normalize(segments, tmp_dir):
    vid = os.path.join(tmp_dir, 'input.mp4')
    out = os.path.join(tmp_dir, 'norm.mp4')
    make_test_video(vid)
    segments.normalize(vid, out, 640, 360, 'yuv420p')
    assert os.path.exists(out)


def test_normalize_with_max_duration(segments, tmp_dir):
    vid = os.path.join(tmp_dir, 'input.mp4')
    out = os.path.join(tmp_dir, 'norm.mp4')
    make_test_video(vid, duration=5)
    segments.normalize(vid, out, 640, 360, 'yuv420p', max_duration=2)
    from mtslinker.prober import MediaProber
    dur = MediaProber().get_duration(out)
    assert dur < 3.0
