import logging
import os
import shutil
import subprocess
from typing import List, Union

from mtslinker.ffmpeg import FFmpegRunner
from mtslinker.prober import MediaProber
from mtslinker.timeline import AudioTrack


class AudioMerger:
    """Merges multiple audio tracks onto a video using batched amix."""

    BATCH_SIZE = 8

    def __init__(self, ffmpeg: FFmpegRunner, prober: MediaProber):
        self.ffmpeg = ffmpeg
        self.prober = prober

    def merge(self, video_path: str,
              audio_files: List[Union[AudioTrack, tuple]],
              tmp_dir: str, output_path: str,
              total_duration: float = 0) -> str:
        video_duration = total_duration or self.prober.get_duration(video_path)

        # Normalize to AudioTrack objects
        tracks = []
        for af in audio_files:
            if isinstance(af, AudioTrack):
                tracks.append(af)
            else:
                tracks.append(AudioTrack(path=af[0], start_time=af[1]))

        # Step 1: Mix in batches with inline adelay
        batch_outputs = []
        total_batches = (len(tracks) + self.BATCH_SIZE - 1) // self.BATCH_SIZE
        for batch_idx, batch_start in enumerate(
            range(0, len(tracks), self.BATCH_SIZE)
        ):
            batch = tracks[batch_start:batch_start + self.BATCH_SIZE]
            batch_out = os.path.join(tmp_dir, f'audio_batch_{batch_idx}.m4a')

            inputs = []
            filter_parts = []
            mix_labels = []
            for j, track in enumerate(batch):
                inputs.extend(['-i', track.path])
                delay_ms = int(track.start_time * 1000)
                label = f'a{j}'
                filter_parts.append(
                    f'[{j}:a]adelay={delay_ms}|{delay_ms},'
                    f'apad=whole_dur={video_duration},'
                    f'atrim=0:{video_duration},'
                    f'asetpts=PTS-STARTPTS[{label}]'
                )
                mix_labels.append(f'[{label}]')

            if len(batch) == 1:
                filter_graph = filter_parts[0].rsplit('[', 1)[0]
            else:
                filter_graph = (
                    ';'.join(filter_parts) + ';'
                    + ''.join(mix_labels)
                    + f'amix=inputs={len(batch)}:duration=longest:normalize=0'
                )

            try:
                self.ffmpeg.run(
                    [
                        'ffmpeg', '-y', '-v', 'error',
                        *inputs,
                        '-filter_complex', filter_graph,
                        '-c:a', 'aac', '-b:a', '128k',
                        '-ar', '44100', '-ac', '2',
                        batch_out,
                    ],
                    description=f'amix batch {batch_idx+1} '
                                f'({len(batch)} tracks, offset {batch[0].start_time:.0f}-'
                                f'{batch[-1].start_time:.0f}s)',
                )
                batch_outputs.append(batch_out)
            except subprocess.CalledProcessError:
                logging.warning(
                    f'Audio batch {batch_idx+1} failed, skipping {len(batch)} tracks'
                )
            logging.info(f'Audio batch {batch_idx+1}/{total_batches} done')

        if not batch_outputs:
            logging.warning('All audio batches failed, skipping audio overlay')
            shutil.move(video_path, output_path)
            return output_path

        # Step 2: Tree-reduce batch outputs
        round_num = 0
        current_paths = batch_outputs
        while len(current_paths) > 1:
            round_num += 1
            next_paths = []
            for batch_start in range(0, len(current_paths), self.BATCH_SIZE):
                batch = current_paths[batch_start:batch_start + self.BATCH_SIZE]
                if len(batch) == 1:
                    next_paths.append(batch[0])
                    continue

                batch_out = os.path.join(
                    tmp_dir, f'audio_reduce_r{round_num}_b{batch_start}.m4a'
                )
                inputs = []
                for bp in batch:
                    inputs.extend(['-i', bp])

                labels = ''.join(f'[{j}:a]' for j in range(len(batch)))
                amix_filter = (
                    f'{labels}amix=inputs={len(batch)}'
                    f':duration=longest:normalize=0'
                )

                self.ffmpeg.run(
                    [
                        'ffmpeg', '-y', '-v', 'error',
                        *inputs,
                        '-filter_complex', amix_filter,
                        '-c:a', 'aac', '-b:a', '128k',
                        '-ar', '44100', '-ac', '2',
                        batch_out,
                    ],
                    description=f'amix reduce round {round_num}, {len(batch)} tracks',
                )
                next_paths.append(batch_out)

                for bp in batch:
                    try:
                        os.remove(bp)
                    except OSError:
                        pass

            logging.info(
                f'Audio reduce round {round_num}: {len(current_paths)} -> '
                f'{len(next_paths)} tracks'
            )
            current_paths = next_paths

        mixed_audio_path = current_paths[0]

        # Step 3: Overlay mixed audio onto video
        logging.info('Overlaying mixed audio onto video...')
        video_has_audio = self.prober.has_audio(video_path)
        if video_has_audio:
            # Mix video's audio with merged audio, normalize sample rates
            filter_graph = (
                '[0:a]aresample=44100[va];'
                '[va][1:a]amix=inputs=2:duration=first:normalize=0[aout]'
            )
            audio_map = ['-map', '[aout]']
        else:
            # Video has no audio, just use merged audio
            audio_map = ['-map', '1:a']
            filter_graph = None

        cmd = [
            'ffmpeg', '-y', '-v', 'warning',
            '-i', video_path,
            '-i', mixed_audio_path,
        ]
        if filter_graph:
            cmd.extend(['-filter_complex', filter_graph])
        cmd.extend([
            '-map', '0:v', *audio_map,
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k',
            output_path,
        ])
        self.ffmpeg.run(cmd, description='overlay mixed audio onto video')
        return output_path
