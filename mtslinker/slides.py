import logging
import os
import shutil
import subprocess
from typing import Dict, List

from mtslinker.ffmpeg import FFmpegRunner


class SlideCompositor:
    """Composites presentation slides alongside webcam video."""

    CANVAS_W = 1280
    CANVAS_H = 720
    SLIDE_W = 960
    CAM_W = 320
    CHUNK_SECS = 1800
    # Exit codes that mean the kernel killed ffmpeg for memory. Python's
    # subprocess reports SIGKILL as -9; some shells/wrappers surface it as
    # 128+9 = 137. Both indicate the same OOM-kill scenario.
    _OOM_KILL_CODES = (-9, 137)

    def __init__(self, ffmpeg: FFmpegRunner):
        self.ffmpeg = ffmpeg

    def composite(self, video_path: str, slide_events: List[Dict],
                  output_path: str, tmp_dir: str, total_duration: float) -> str:
        logging.info(f'Compositing {len(slide_events)} slide changes onto video...')

        slide_track_path = self._build_slide_track(slide_events, tmp_dir, total_duration)

        filter_graph = (
            f'[0:v]fps=25,scale={self.CAM_W}:-2,setsar=1[webcam];'
            f'[1:v]fps=25,scale={self.SLIDE_W}:{self.CANVAS_H}:'
            f'force_original_aspect_ratio=decrease,'
            f'pad={self.SLIDE_W}:{self.CANVAS_H}:(ow-iw)/2:(oh-ih)/2:white[slide];'
            f'color=c=black:s={self.CANVAS_W}x{self.CANVAS_H}:r=25[bg];'
            f'[bg][slide]overlay=0:0[tmp];'
            f'[tmp][webcam]overlay={self.SLIDE_W}:0[out]'
        )

        n_chunks = max(1, int(total_duration + self.CHUNK_SECS - 1) // self.CHUNK_SECS)

        if n_chunks == 1:
            cmd = [
                'ffmpeg', '-y', '-v', 'warning',
                '-i', video_path, '-i', slide_track_path,
                '-filter_complex', filter_graph,
                '-map', '[out]', '-map', '0:a?',
                *self.ffmpeg.get_video_encoder(),
                '-c:a', 'copy', '-r', '25', '-shortest',
                output_path,
            ]
            self.ffmpeg.run(cmd, description='composite slides + webcam')
        else:
            logging.info(f'Compositing in {n_chunks} chunks of {self.CHUNK_SECS}s to avoid OOM')
            chunks_dir = os.path.join(tmp_dir, 'composite_chunks')
            os.makedirs(chunks_dir, exist_ok=True)
            chunk_paths = []
            # NVENC keeps pinned host buffers per stream; on a long, heavy
            # filter_complex graph the OOM-killer can SIGKILL ffmpeg mid
            # chunk. Once that has happened, the NVENC budget for *this*
            # video is over budget — stay on CPU for the rest of the chunks
            # instead of paying the kill cost again on each one.
            use_cpu_encoder = False

            def build_chunk_cmd(ss_arg, chunk_path_arg, enc_args):
                return [
                    'ffmpeg', '-y', '-v', 'warning',
                    '-ss', str(ss_arg), '-t', str(self.CHUNK_SECS),
                    '-i', video_path,
                    '-ss', str(ss_arg), '-t', str(self.CHUNK_SECS),
                    '-i', slide_track_path,
                    '-filter_complex', filter_graph,
                    '-map', '[out]', '-map', '0:a?',
                    *enc_args, '-c:a', 'aac', '-b:a', '192k',
                    '-r', '25', '-shortest',
                    chunk_path_arg,
                ]

            for ci in range(n_chunks):
                ss = ci * self.CHUNK_SECS
                chunk_path = os.path.join(chunks_dir, f'chunk_{ci:03d}.mp4')
                label = f'composite chunk {ci+1}/{n_chunks}'

                if use_cpu_encoder:
                    self.ffmpeg.run(
                        build_chunk_cmd(ss, chunk_path,
                                        self.ffmpeg.get_video_encoder_cpu()),
                        description=f'{label} (cpu)',
                    )
                else:
                    try:
                        self.ffmpeg.run(
                            build_chunk_cmd(ss, chunk_path,
                                            self.ffmpeg.get_video_encoder()),
                            description=label,
                        )
                    except subprocess.CalledProcessError as e:
                        if e.returncode not in self._OOM_KILL_CODES:
                            raise
                        logging.warning(
                            f'{label} OOM-killed (exit {e.returncode}); '
                            f'retrying this chunk with CPU encoder and '
                            f'falling back to CPU for any remaining chunks'
                        )
                        use_cpu_encoder = True
                        self.ffmpeg.run(
                            build_chunk_cmd(ss, chunk_path,
                                            self.ffmpeg.get_video_encoder_cpu()),
                            description=f'{label} (cpu retry)',
                        )
                chunk_paths.append(chunk_path)
                logging.info(f'Composite chunk {ci+1}/{n_chunks} done')

            chunk_list = os.path.join(chunks_dir, 'concat.txt')
            with open(chunk_list, 'w') as f:
                for cp in chunk_paths:
                    f.write(f"file '{os.path.abspath(cp)}'\n")
            self.ffmpeg.run(
                ['ffmpeg', '-y', '-v', 'error',
                 '-f', 'concat', '-safe', '0', '-i', chunk_list,
                 '-c', 'copy', output_path],
                description='concat composite chunks',
            )
            shutil.rmtree(chunks_dir, ignore_errors=True)

        logging.info('Slide compositing complete')
        slides_dir = os.path.join(tmp_dir, 'slide_segments')
        shutil.rmtree(slides_dir, ignore_errors=True)
        return output_path

    def _build_slide_track(self, slide_events, tmp_dir, total_duration):
        slides_dir = os.path.join(tmp_dir, 'slide_segments')
        os.makedirs(slides_dir, exist_ok=True)

        slide_segments = []
        for i, se in enumerate(slide_events):
            t_start = se['time']
            t_end = slide_events[i + 1]['time'] if i + 1 < len(slide_events) else total_duration
            duration = t_end - t_start
            if duration <= 0:
                continue

            seg_path = os.path.join(slides_dir, f'seg_{i}.mp4')
            n_frames = max(1, int(duration))
            self.ffmpeg.run(
                [
                    'ffmpeg', '-y', '-v', 'error',
                    '-loop', '1', '-framerate', '1',
                    '-i', se['local_path'],
                    '-vf', f'scale={self.SLIDE_W}:{self.CANVAS_H}:'
                           f'force_original_aspect_ratio=decrease,'
                           f'pad={self.SLIDE_W}:{self.CANVAS_H}:(ow-iw)/2:(oh-ih)/2:white',
                    *self.ffmpeg.get_video_encoder_fast(),
                    '-pix_fmt', 'yuv420p',
                    '-r', '1', '-frames:v', str(n_frames),
                    seg_path,
                ],
                description=f'slide segment {i+1}/{len(slide_events)} '
                            f'({n_frames} frames, {duration:.0f}s)',
            )
            slide_segments.append(seg_path)

        first_time = slide_events[0]['time'] if slide_events else 0
        if first_time > 0.5:
            leader_path = os.path.join(slides_dir, 'leader.mp4')
            self.ffmpeg.run(
                [
                    'ffmpeg', '-y', '-v', 'error',
                    '-f', 'lavfi', '-i',
                    f'color=c=black:s={self.SLIDE_W}x{self.CANVAS_H}:d={first_time}:r=1',
                    *self.ffmpeg.get_video_encoder_fast(),
                    '-pix_fmt', 'yuv420p',
                    leader_path,
                ],
                description='slide leader (black)',
            )
            slide_segments.insert(0, leader_path)

        slide_track_path = os.path.join(tmp_dir, 'slide_track.mp4')
        concat_list = os.path.join(slides_dir, 'concat.txt')
        with open(concat_list, 'w') as f:
            for seg in slide_segments:
                f.write(f"file '{os.path.abspath(seg)}'\n")

        self.ffmpeg.run(
            ['ffmpeg', '-y', '-v', 'error',
             '-f', 'concat', '-safe', '0', '-i', concat_list,
             '-c', 'copy', slide_track_path],
            description='concat slide track',
        )
        logging.info('Slide track created')
        return slide_track_path
