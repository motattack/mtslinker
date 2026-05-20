from mtslinker.processor import VideoProcessor
from mtslinker.ffmpeg import FFmpegRunner
from mtslinker.prober import MediaProber
from mtslinker.segments import SegmentBuilder
from mtslinker.grid import GridCompositor
from mtslinker.slides import SlideCompositor
from mtslinker.audio import AudioMerger


def test_processor_composes_all_classes():
    p = VideoProcessor()
    assert p.ffmpeg is not None
    assert p.prober is not None
    assert p.segments is not None
    assert p.grid is not None
    assert p.slides is not None
    assert p.audio is not None


def test_segments_uses_injected_ffmpeg():
    p = VideoProcessor()
    assert p.segments.ffmpeg is p.ffmpeg
    assert p.segments.prober is p.prober


def test_grid_uses_injected_ffmpeg():
    p = VideoProcessor()
    assert p.grid.ffmpeg is p.ffmpeg


def test_slides_uses_injected_ffmpeg():
    p = VideoProcessor()
    assert p.slides.ffmpeg is p.ffmpeg


def test_audio_uses_injected_deps():
    p = VideoProcessor()
    assert p.audio.ffmpeg is p.ffmpeg
    assert p.audio.prober is p.prober
