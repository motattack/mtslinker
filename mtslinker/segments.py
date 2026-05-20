import os
import subprocess

from mtslinker.ffmpeg import FFmpegRunner
from mtslinker.prober import MediaProber


class SegmentBuilder:
    """Builds, normalizes, and deduplicates video segments."""

    def __init__(self, ffmpeg: FFmpegRunner, prober: MediaProber):
        self.ffmpeg = ffmpeg
        self.prober = prober

    def generate_black(self, output_path: str, duration: float,
                       width: int = 1920, height: int = 1080,
                       pix_fmt: str = 'yuv420p') -> str:
        self.ffmpeg.run(
            [
                'ffmpeg', '-y', '-v', 'error',
                '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:d={duration}:r=25',
                '-f', 'lavfi', '-i', f'anullsrc=r=44100:cl=stereo',
                '-t', str(duration),
                *self.ffmpeg.get_video_encoder_fast(),
                '-pix_fmt', pix_fmt,
                '-c:a', 'aac', '-b:a', '128k',
                '-shortest',
                output_path,
            ],
            description=f'generate black segment ({duration:.1f}s)',
        )
        return output_path

    def ensure_audio(self, input_path: str, output_path: str) -> str:
        info = self.prober.probe_streams(input_path)
        has_audio = any(
            s.get('codec_type') == 'audio' for s in info.get('streams', [])
        )
        if has_audio:
            return input_path
        # anullsrc MUST be bounded by an input-side -t equal to the video
        # length. With an unbounded anullsrc and -c:v copy, if the input has
        # no decodable video the muxer waits forever for a video packet to
        # interleave against, buffering silent audio without bound until
        # "av_interleaved_write_frame: Cannot allocate memory" (observed in
        # production on a normalize() output that seeked past end-of-source).
        # A zero/unknown duration means the input is empty or corrupt, so
        # there is no safe silent track to synthesize: fail loudly (the
        # callers fall back to a black gap) instead of running unbounded.
        duration = self.prober.get_duration(input_path)
        if duration <= 0:
            raise subprocess.CalledProcessError(
                1, 'ensure_audio', output=None,
                stderr=(f'refusing silent-audio mux: {input_path} has no '
                        f'positive duration (empty or corrupt input)'),
            )
        self.ffmpeg.run(
            [
                'ffmpeg', '-y', '-v', 'error',
                '-i', input_path,
                '-f', 'lavfi', '-t', str(duration),
                '-i', 'anullsrc=r=44100:cl=stereo',
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
                '-shortest', '-max_interleave_delta', '0',
                output_path,
            ],
            description='add silent audio stream',
        )
        return output_path

    def normalize(self, input_path: str, output_path: str,
                  width: int, height: int, pix_fmt: str,
                  max_duration: float = 0, seek: float = 0) -> str:
        seek_args = ['-ss', str(seek)] if seek > 0 else []
        duration_args = ['-t', str(max_duration)] if max_duration > 0 else []
        self.ffmpeg.run(
            [
                'ffmpeg', '-y', '-v', 'error',
                *seek_args,
                '-i', input_path,
                '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                       f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1',
                '-pix_fmt', pix_fmt,
                *self.ffmpeg.get_video_encoder_fast(),
                '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
                '-r', '25',
                *duration_args,
                output_path,
            ],
            description=f'normalize segment {os.path.basename(input_path)}',
        )
        # ffmpeg exits 0 even when -ss seeks past end-of-source and zero
        # frames are decoded, leaving a tiny but valid empty container.
        # Treat a zero-duration normalize as a failure so the caller falls
        # back to a black gap instead of feeding an empty file into
        # ensure_audio (whose muxer would then OOM).
        if self.prober.get_duration(output_path) <= 0:
            raise subprocess.CalledProcessError(
                1, 'normalize', output=None,
                stderr=(f'normalize produced empty output for {input_path} '
                        f'(seek {seek}s past end-of-source?)'),
            )
        return output_path


