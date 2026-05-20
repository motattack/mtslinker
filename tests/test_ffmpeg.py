import subprocess
import pytest
from mtslinker.ffmpeg import FFmpegRunner


def test_detect_gpu_sets_flags(ffmpeg):
    ffmpeg.detect_gpu()
    assert ffmpeg.nvenc_available is not None
    assert ffmpeg.cuda_overlay_available is not None


def test_detect_gpu_idempotent(ffmpeg):
    ffmpeg.detect_gpu()
    first = ffmpeg.nvenc_available
    ffmpeg.detect_gpu()
    assert ffmpeg.nvenc_available == first


def test_get_video_encoder_returns_codec_flag(ffmpeg):
    enc = ffmpeg.get_video_encoder()
    assert '-c:v' in enc
    assert len(enc) >= 2


def test_get_video_encoder_fast_returns_codec_flag(ffmpeg):
    enc = ffmpeg.get_video_encoder_fast()
    assert '-c:v' in enc


def test_get_video_encoder_cpu_always_returns_libx264(ffmpeg):
    """The CPU fallback must never pick NVENC, even when NVENC is available.

    It exists specifically to bypass a system OOM-kill of NVENC, so it
    cannot conditionally fall back to the very thing it is meant to
    replace.
    """
    enc = ffmpeg.get_video_encoder_cpu()
    assert enc[enc.index('-c:v') + 1] == 'libx264'


def test_run_success(ffmpeg):
    result = ffmpeg.run(['ffmpeg', '-version'], description='version')
    assert result.returncode == 0


def test_run_failure_raises(ffmpeg):
    with pytest.raises(subprocess.CalledProcessError):
        ffmpeg.run(['ffmpeg', '-i', '/nonexistent_file.mp4'], description='bad')
