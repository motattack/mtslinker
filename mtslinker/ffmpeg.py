import logging
import shutil
import subprocess


class FFmpegRunner:
    """Handles ffmpeg/ffprobe execution and GPU detection."""

    def __init__(self):
        self.nvenc_available = None
        self.cuda_overlay_available = None
        self._check_tools()

    def _check_tools(self):
        for tool in ('ffmpeg', 'ffprobe'):
            if not shutil.which(tool):
                raise RuntimeError(
                    f'{tool} is not installed. '
                    'Install it with: apt install ffmpeg / brew install ffmpeg'
                )

    def detect_gpu(self):
        if self.nvenc_available is not None:
            return
        self.nvenc_available = self._has_nvenc()
        self.cuda_overlay_available = (
            self._has_cuda_overlay() if self.nvenc_available else False
        )
        if self.cuda_overlay_available:
            logging.info('CUDA overlay + NVENC detected, using full GPU pipeline')
        elif self.nvenc_available:
            logging.info('NVENC detected (no CUDA overlay), using GPU encoder only')
        else:
            logging.info('No GPU support, using CPU pipeline')

    def _has_nvenc(self) -> bool:
        try:
            result = subprocess.run(
                ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                 'nullsrc=s=64x64:d=0.1', '-c:v', 'h264_nvenc', '-f', 'null', '-'],
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _has_cuda_overlay(self) -> bool:
        try:
            result = subprocess.run(
                ['ffmpeg', '-v', 'error', '-filters'],
                capture_output=True, text=True, timeout=10,
            )
            return 'overlay_cuda' in result.stdout
        except Exception:
            return False

    def get_video_encoder(self) -> list:
        self.detect_gpu()
        if self.nvenc_available:
            return ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '23']
        return ['-c:v', 'libx264', '-preset', 'fast', '-crf', '23']

    def get_video_encoder_fast(self) -> list:
        self.detect_gpu()
        if self.nvenc_available:
            return ['-c:v', 'h264_nvenc', '-preset', 'p1', '-cq', '28']
        return ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23']

    def get_video_encoder_cpu(self) -> list:
        # Fallback for callers that need to bypass NVENC after a system
        # OOM-kill — NVENC's pinned host buffers can push long filter_complex
        # jobs over the memory budget; libx264 fits in less RAM at the cost
        # of CPU time.
        return ['-c:v', 'libx264', '-preset', 'fast', '-crf', '23']

    def run(self, cmd: list, description: str = 'ffmpeg'):
        logging.debug(f'Running {description}: {" ".join(cmd[:6])}...')
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else '(no stderr)'
            logging.error(f'{description} failed (exit {result.returncode}):\n{stderr}')
            raise subprocess.CalledProcessError(
                result.returncode, cmd[0], result.stdout, result.stderr
            )
        if result.stderr and result.stderr.strip():
            logging.debug(f'{description} stderr: {result.stderr.strip()[:500]}')
        return result
