import os
import subprocess

import pytest

from mtslinker.slides import SlideCompositor


class _FakeFFmpeg:
    """Composition substitute for FFmpegRunner used in SlideCompositor tests.

    Records every (cmd, description) call. To make the next call matching
    a description substring fail with a specific returncode, register it
    via fail_with() before invoking the code under test.
    """

    def __init__(self):
        self.calls = []
        self._pending_failures = []
        self.nvenc_available = True
        self.cuda_overlay_available = False

    def get_video_encoder(self):
        return ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '23']

    def get_video_encoder_fast(self):
        return ['-c:v', 'h264_nvenc', '-preset', 'p1', '-cq', '28']

    def get_video_encoder_cpu(self):
        return ['-c:v', 'libx264', '-preset', 'fast', '-crf', '23']

    def fail_with(self, description_substring, returncode):
        self._pending_failures.append((description_substring, returncode))

    def run(self, cmd, description='ffmpeg'):
        self.calls.append((list(cmd), description))
        for i, (sub, rc) in enumerate(self._pending_failures):
            if sub == description:
                self._pending_failures.pop(i)
                raise subprocess.CalledProcessError(rc, cmd[0], '', '')
        out = cmd[-1]
        if isinstance(out, str) and not out.startswith('-'):
            try:
                with open(out, 'wb') as f:
                    f.write(b'\x00')
            except OSError:
                pass


class _StubSlideTrackCompositor(SlideCompositor):
    """SlideCompositor with the slide-track build stubbed out.

    The OOM-retry logic under test lives in composite()'s chunked branch;
    _build_slide_track does many small ffmpeg calls that would clutter the
    recorded call list and tell us nothing about retry behavior. Touching
    a single empty file is enough — the chunked composite reads the path,
    not the contents.
    """

    def _build_slide_track(self, slide_events, tmp_dir, total_duration):
        path = os.path.join(tmp_dir, 'slide_track.mp4')
        open(path, 'wb').close()
        return path


def _enc_codec(cmd):
    return cmd[cmd.index('-c:v') + 1]


def _composite_chunk_calls(calls):
    # Match the per-chunk encode calls only; the final 'concat composite
    # chunks' concat-demuxer step also contains "composite chunk" as a
    # substring and would otherwise pollute the assertions.
    return [(c, d) for c, d in calls if d.startswith('composite chunk')]


def _slide_events():
    return [{'time': 0, 'local_path': 'x.jpg', 'slide_number': 1}]


def test_composite_chunk_retries_on_oom_kill_with_cpu_encoder(tmp_path):
    """exit -9 SIGKILL on NVENC must trigger a CPU retry of that chunk.

    Reproduces the production OOM-kill on `composite chunk 2/8` (URL
    ...record-new/2413910825): the slide compositor's NVENC pipeline was
    killed by the kernel OOM killer mid-chunk. The correct response is to
    retry that chunk with libx264 and stay on CPU for the rest of the
    video so the next chunk does not pay the kill cost again.
    """
    ffmpeg = _FakeFFmpeg()
    ffmpeg.fail_with('composite chunk 2/3', -9)
    compositor = _StubSlideTrackCompositor(ffmpeg)

    video = os.path.join(str(tmp_path), 'video.mp4')
    open(video, 'wb').close()
    out = os.path.join(str(tmp_path), 'final.mp4')
    compositor.composite(video, _slide_events(), out, str(tmp_path), 5400.0)

    chunks = _composite_chunk_calls(ffmpeg.calls)
    descs = [d for _, d in chunks]
    assert descs == [
        'composite chunk 1/3',
        'composite chunk 2/3',
        'composite chunk 2/3 (cpu retry)',
        'composite chunk 3/3 (cpu)',
    ], descs
    assert _enc_codec(chunks[0][0]) == 'h264_nvenc'
    assert _enc_codec(chunks[1][0]) == 'h264_nvenc'
    assert _enc_codec(chunks[2][0]) == 'libx264'
    assert _enc_codec(chunks[3][0]) == 'libx264'


def test_composite_chunk_does_not_retry_on_non_oom_failure(tmp_path):
    """A regular nonzero exit must propagate — only SIGKILL means OOM.

    Falling back to CPU on every ffmpeg error would hide real bugs (bad
    inputs, codec errors, etc.). Only the kernel OOM killer signals
    memory pressure, so only that should trigger the silent retry.
    """
    ffmpeg = _FakeFFmpeg()
    ffmpeg.fail_with('composite chunk 1/3', 1)
    compositor = _StubSlideTrackCompositor(ffmpeg)

    video = os.path.join(str(tmp_path), 'video.mp4')
    open(video, 'wb').close()
    out = os.path.join(str(tmp_path), 'final.mp4')
    with pytest.raises(subprocess.CalledProcessError):
        compositor.composite(video, _slide_events(), out, str(tmp_path), 5400.0)

    chunks = _composite_chunk_calls(ffmpeg.calls)
    assert len(chunks) == 1
    assert chunks[0][1] == 'composite chunk 1/3'


def test_composite_chunk_treats_exit_137_as_oom_kill(tmp_path):
    """Some shells surface SIGKILL as 128+9=137 instead of Python's -9.

    Both must trigger the same CPU retry path so the fallback is not
    sensitive to the kernel/wrapper that reaped ffmpeg.
    """
    ffmpeg = _FakeFFmpeg()
    ffmpeg.fail_with('composite chunk 1/3', 137)
    compositor = _StubSlideTrackCompositor(ffmpeg)

    video = os.path.join(str(tmp_path), 'video.mp4')
    open(video, 'wb').close()
    out = os.path.join(str(tmp_path), 'final.mp4')
    compositor.composite(video, _slide_events(), out, str(tmp_path), 5400.0)

    chunks = _composite_chunk_calls(ffmpeg.calls)
    descs = [d for _, d in chunks]
    assert descs == [
        'composite chunk 1/3',
        'composite chunk 1/3 (cpu retry)',
        'composite chunk 2/3 (cpu)',
        'composite chunk 3/3 (cpu)',
    ], descs


def test_composite_chunk_uses_nvenc_when_nothing_fails(tmp_path):
    """The happy path must keep using NVENC — the CPU fallback is only
    engaged after an actual OOM-kill, never preemptively."""
    ffmpeg = _FakeFFmpeg()
    compositor = _StubSlideTrackCompositor(ffmpeg)

    video = os.path.join(str(tmp_path), 'video.mp4')
    open(video, 'wb').close()
    out = os.path.join(str(tmp_path), 'final.mp4')
    compositor.composite(video, _slide_events(), out, str(tmp_path), 5400.0)

    chunks = _composite_chunk_calls(ffmpeg.calls)
    assert [d for _, d in chunks] == [
        'composite chunk 1/3',
        'composite chunk 2/3',
        'composite chunk 3/3',
    ]
    for cmd, _ in chunks:
        assert _enc_codec(cmd) == 'h264_nvenc'
