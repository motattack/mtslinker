import os
import shutil
import subprocess
import tempfile
import pytest

from mtslinker.ffmpeg import FFmpegRunner
from mtslinker.prober import MediaProber
from mtslinker.segments import SegmentBuilder


@pytest.fixture
def ffmpeg():
    return FFmpegRunner()


@pytest.fixture
def prober():
    return MediaProber()


@pytest.fixture
def segments(ffmpeg, prober):
    return SegmentBuilder(ffmpeg, prober)


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_test_video(path, duration=3, with_audio=True, width=320, height=240):
    audio_args = []
    if with_audio:
        audio_args = ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}',
                      '-c:a', 'aac', '-shortest']
    subprocess.run(
        ['ffmpeg', '-y', '-v', 'error',
         '-f', 'lavfi', '-i', f'color=red:s={width}x{height}:d={duration}:r=25',
         *audio_args,
         '-c:v', 'libx264', '-preset', 'ultrafast', path],
        capture_output=True, check=True,
    )


def make_test_audio(path, duration=3, frequency=880):
    subprocess.run(
        ['ffmpeg', '-y', '-v', 'error',
         '-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration={duration}',
         '-c:a', 'aac', path],
        capture_output=True, check=True,
    )
