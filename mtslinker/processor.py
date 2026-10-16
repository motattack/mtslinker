import logging
import json
import os
import platform
import subprocess
from typing import Dict, Tuple, List, Union

import numpy as np
import tqdm
from moviepy.audio.AudioClip import AudioArrayClip, CompositeAudioClip
from moviepy.audio.io.AudioFileClip import AudioFileClip
from moviepy.video.VideoClip import ColorClip
from moviepy import VideoFileClip, concatenate_videoclips

import warnings
warnings.simplefilter("ignore")


from mtslinker.downloader import download_video_chunks

# 25 FPS is sufficient for lectures and screen recordings and reduces the
# number of frames that must be filtered and encoded.
TARGET_FPS = 25
TARGET_AUDIO_FPS = 44100
AUDIO_LOUDNORM = 'loudnorm=I=-14:TP=-1.0:LRA=11'
# 450 kbps is sufficient for a tiny preview, but produces visibly degraded
# 360p lecture video. The actual floor is selected from the output dimensions
# below; this is only the absolute safety floor for unknown dimensions.
MIN_OUTPUT_VIDEO_BITRATE_KBPS = 900
MAX_OUTPUT_VIDEO_BITRATE_KBPS = 6000


def _set_clip_fps(clip, fps):
    """Set a clip's sampling rate using the API available in MoviePy 1 or 2."""
    if hasattr(clip, 'with_fps'):
        return clip.with_fps(fps)
    return clip.set_fps(fps)


def _has_video_stream(path):
    """Return whether ffprobe sees a video stream, or ``None`` if unavailable."""
    try:
        probe = subprocess.run(
            [
                'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if probe.returncode != 0:
        return None
    return 'video' in probe.stdout.split()


def _open_video_clip(path):
    """Open a video fragment, tolerating files with incomplete FPS metadata."""
    try:
        return VideoFileClip(path, fps_source='fps')
    except (KeyError, OSError, IndexError, ValueError, TypeError):
        # Some MTS fragments contain a valid video stream but omit the
        # ``fps`` field used by MoviePy. ``tbr`` is available in the same
        # ffmpeg probe and is a safe fallback for those files.
        return VideoFileClip(path, fps_source='tbr')


def _select_video_encoder(target_bitrate_kbps):
    """Select an available hardware encoder, falling back to libx264."""
    try:
        probe = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True,
            text=True,
            check=False,
        )
        encoders = probe.stdout + probe.stderr
    except OSError:
        encoders = ''

    # Keep the availability probe close to the command users run manually.
    # In particular, ``-b:v 0`` and the quality-rate-control combination below
    # can fail on an otherwise working NVENC installation (driver/ffmpeg
    # builds differ in the supported option set). Bitrate control is added
    # only after the encoder has passed this minimal initialization test.
    candidates = []
    if platform.system() == 'Darwin' and 'h264_videotoolbox' in encoders:
        candidates.append(('h264_videotoolbox', ['-pix_fmt', 'yuv420p']))
    if 'h264_nvenc' in encoders:
        candidates.append(('h264_nvenc', ['-preset', 'p2', '-pix_fmt', 'yuv420p']))
    for encoder, options in candidates:
        test = subprocess.run(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                '-i', 'testsrc2=size=1280x720:rate=30', '-t', '1', '-c:v', encoder,
                *options, '-f', 'null', '-'
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if test.returncode == 0:
            target = f'{target_bitrate_kbps}k'
            options = [*options, '-b:v', target, '-maxrate', target,
                       '-bufsize', f'{target_bitrate_kbps * 2}k']
            return encoder, options
        logging.warning('%s is listed but unavailable; using CPU encoding.', encoder)
    return 'libx264', [
        '-preset', 'ultrafast', '-b:v', f'{target_bitrate_kbps}k',
        '-maxrate', f'{target_bitrate_kbps}k', '-bufsize', f'{target_bitrate_kbps * 2}k'
    ]


def _target_video_bitrate_kbps(video_clips):
    source_bitrates = []
    for clip in video_clips:
        reader = getattr(clip, 'reader', None)
        infos = getattr(reader, 'infos', {}) if reader else {}
        bitrate = infos.get('video_bitrate')
        if bitrate:
            source_bitrates.append(float(bitrate))
    source_bitrate = max(source_bitrates, default=1000.0)
    target = round(source_bitrate * 1.5)

    # Use a resolution-aware floor so a low or missing source bitrate does not
    # turn a normal lecture recording into a blocky 450 kbps encode.
    width, height = getattr(video_clips[0], 'size', (0, 0))
    pixels = int(width or 0) * int(height or 0)
    if pixels <= 640 * 360:
        quality_floor = 1200
    elif pixels <= 1280 * 720:
        quality_floor = 2500
    else:
        quality_floor = 4000
    quality_floor = max(MIN_OUTPUT_VIDEO_BITRATE_KBPS, quality_floor)
    return max(quality_floor, min(MAX_OUTPUT_VIDEO_BITRATE_KBPS, target))


def _audio_group(stream, fallback_index):
    """Return a stable source key and a human-readable track label."""
    if isinstance(stream, dict):
        for stream_type in ('conference', 'screensharing'):
            if stream_type not in stream:
                continue
            value = stream[stream_type]
            if isinstance(value, dict):
                identity = next(
                    (value.get(name) for name in ('id', 'streamId', 'participantId', 'userId') if value.get(name)),
                    None,
                )
            else:
                identity = value
            if identity is not None:
                key = f'{stream_type}:{identity}'
            else:
                key = f'{stream_type}:{json.dumps(value, sort_keys=True, ensure_ascii=False)}'
            return key, 'Лектор' if stream_type == 'conference' else 'Трансляция экрана'
    return f'audio-source-{fallback_index}', 'Аудио'


def process_video_clips(
    directory: str,
    json_data: Dict,
    download_workers: int = 8,
) -> Tuple[float, List[VideoFileClip], List[AudioFileClip]]:
    total_duration = float(json_data.get('duration', 0))
    if not total_duration:
        raise ValueError('Duration not found in JSON data.')

    video_clips = []
    audio_clips = []
    events = []

    for event in json_data.get('eventLogs', []):
        if isinstance(event, dict):
            data = event.get('data', {})
            if isinstance(data, dict) and 'url' in data:
                url = data['url']
                start_time = event.get('relativeTime', 0)
                events.append((url, start_time, data.get('stream')))

    fragments_directory = os.path.join(directory, 'fragments')
    os.makedirs(fragments_directory, exist_ok=True)
    logging.info('Fragment directory: %s', fragments_directory)
    downloaded_paths = download_video_chunks(
        (url for url, _, _ in events), fragments_directory, max_workers=download_workers
    )
    audio_source_index = 0
    for downloaded_file_path, (_, start_time, stream) in zip(downloaded_paths, events):
        has_video = _has_video_stream(downloaded_file_path)
        if has_video is not False:
            try:
                video_clip = _set_clip_fps(_open_video_clip(downloaded_file_path), TARGET_FPS)
                video_clip = video_clip.with_start(start_time)
                video_clips.append(video_clip)
                continue
            except (KeyError, OSError, IndexError, ValueError, TypeError) as error:
                if has_video:
                    raise RuntimeError(
                        f'Video fragment contains a video stream but could not be opened: '
                        f'{downloaded_file_path}'
                    ) from error

        try:
            audio_clip = _set_clip_fps(AudioFileClip(downloaded_file_path), TARGET_AUDIO_FPS)
            audio_clip = audio_clip.with_start(start_time)
            group, label = _audio_group(stream, audio_source_index)
            audio_clip._mts_audio_group = group
            audio_clip._mts_audio_label = label
            audio_source_index += 1
            audio_clips.append(audio_clip)
        except (KeyError, OSError, IndexError, ValueError, TypeError) as error:
            logging.warning('Skipping unreadable fragment %s: %s', downloaded_file_path, error)
    logging.info('Classified fragments: %d video, %d audio.', len(video_clips), len(audio_clips))
    logging.info(f'Total duration of clips: {total_duration}')

    return total_duration, video_clips, audio_clips


def create_video_with_gaps(total_duration: float, video_clips: List[VideoFileClip]) -> VideoFileClip:
    clips = []
    current_time = 0.0

    for video in video_clips:
        if video.start > current_time:
            gap_duration = video.start - current_time
            if gap_duration > 0:
                empty_clip = ColorClip(size=(1920, 1080), color=(0, 0, 0), duration=gap_duration).with_start(
                    current_time)
                clips.append(empty_clip)

        clips.append(video)
        current_time = video.end

    if current_time < total_duration:
        remaining_duration = total_duration - current_time
        empty_clip = ColorClip(size=(1920, 1080), color=(0, 0, 0), duration=remaining_duration).with_start(current_time)
        clips.append(empty_clip)

    # ``compose`` composites every frame onto a canvas and is considerably
    # slower. All clips produced by MTS Link normally have the same size, so
    # use the direct frame chain in that case and retain compose as a safe
    # fallback for recordings with mixed resolutions.
    clip_sizes = {tuple(clip.size) for clip in clips}
    concat_method = 'chain' if len(clip_sizes) == 1 else 'compose'
    logging.info('Concatenating %d clips with method=%s.', len(clips), concat_method)
    final_video = concatenate_videoclips(clips, method=concat_method)
    logging.info(f'Final video duration: {final_video.duration}')
    return final_video


def create_audio_with_gaps(total_duration: float, audio_clips: List[AudioFileClip]) -> CompositeAudioClip:
    audio_segments = []
    current_time = 0

    for audio in audio_clips:
        if audio.start > current_time:
            gap_duration = audio.start - current_time
            if gap_duration > 0:
                silence_segment = AudioArrayClip(
                    np.zeros((int(gap_duration * TARGET_AUDIO_FPS), 2), dtype=np.float32),
                    fps=TARGET_AUDIO_FPS
                ).with_start(current_time)
                audio_segments.append(silence_segment)

        audio_segments.append(audio)
        current_time = audio.end

    if current_time < total_duration:
        remaining_duration = total_duration - current_time
        silence_segment = AudioArrayClip(
            np.zeros((int(remaining_duration * TARGET_AUDIO_FPS), 2), dtype=np.float32),
            fps=TARGET_AUDIO_FPS
        ).with_start(current_time)
        audio_segments.append(silence_segment)

    final_audio = CompositeAudioClip(audio_segments)
    logging.info(f'Total audio duration: {final_audio.duration}')
    return final_audio


def compile_final_video_ffmpeg(
    total_duration: float,
    video_clips: List[VideoFileClip],
    audio_clips: List[AudioFileClip],
    output_path: str,
    include_multitrack: bool = False,
) -> None:
    """Render the timeline in ffmpeg without a Python frame-generation loop."""
    if not video_clips:
        raise ValueError('No video clips available for ffmpeg rendering.')

    target_bitrate_kbps = _target_video_bitrate_kbps(video_clips)
    video_encoder, encoder_options = _select_video_encoder(target_bitrate_kbps)
    logging.info('Using video encoder: %s at target bitrate %dkbps', video_encoder, target_bitrate_kbps)
    width, height = video_clips[0].size
    command = ['ffmpeg', '-y', '-loglevel', 'error']
    for clip in [*video_clips, *audio_clips]:
        filename = getattr(clip, 'filename', None)
        if not filename:
            raise ValueError('A clip has no source filename for ffmpeg rendering.')
        command.extend(['-i', filename])

    filters = [
        f'color=c=black:s={width}x{height}:r={TARGET_FPS}:d={total_duration},'
        f'format=yuv420p[vcanvas0]'
    ]
    canvas_label = 'vcanvas0'
    overlay_index = 0
    for index, clip in enumerate(video_clips):
        start = float(clip.start or 0)
        if start >= total_duration:
            continue
        duration = min(float(clip.duration), total_duration - max(0.0, start))
        if duration <= 0:
            continue

        label = f'v{index}'
        # Normalize every decoded stream. MoviePy's ``clip.size`` can disagree
        # with ffmpeg's actual frame dimensions for rotated or portrait
        # fragments, so conditional scaling is not sufficient.
        video_filter = (
            f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,'
            f'fps={TARGET_FPS},format=yuv420p,trim=duration={duration:.6f},'
            f'setpts=PTS-STARTPTS+{max(0.0, start):.6f}/TB'
        )
        filters.append(f'[{index}:v]{video_filter}[{label}]')
        filters.append(
            f'[{canvas_label}][{label}]overlay=eof_action=pass:repeatlast=0:shortest=0'
            f'[vcanvas{overlay_index + 1}]'
        )
        canvas_label = f'vcanvas{overlay_index + 1}'
        overlay_index += 1

    filters.append(f'[{canvas_label}]null[vout]')
    output_options = ['-map', '[vout]']

    if audio_clips:
        audio_labels = []
        video_count = len(video_clips)
        for index, clip in enumerate(audio_clips):
            input_index = video_count + index
            delay_ms = max(0, round(float(clip.start or 0) * 1000))
            source_label = f'a{index}'
            split = (
                f',asplit=2[{source_label}_master][{source_label}_track]'
                if include_multitrack else f'[{source_label}_master]'
            )
            filters.append(
                f'[{input_index}:a]aresample={TARGET_AUDIO_FPS},asetpts=PTS-STARTPTS,'
                f'adelay={delay_ms}|{delay_ms}{split}'
            )
            audio_labels.append(f'[{source_label}_master]')

        groups = {}
        group_labels = {}
        for index, clip in enumerate(audio_clips):
            key = getattr(clip, '_mts_audio_group', f'audio-source-{index}')
            groups.setdefault(key, []).append(f'[a{index}_track]')
            group_labels.setdefault(key, getattr(clip, '_mts_audio_label', 'Аудио'))

        def add_mix(inputs, label):
            mix = ''.join(inputs)
            if len(inputs) > 1:
                mix += f'amix=inputs={len(inputs)}:duration=longest:dropout_transition=0,'
            mix += (
                f'apad=whole_dur={total_duration},{AUDIO_LOUDNORM},'
                f'atrim=duration={total_duration},aresample={TARGET_AUDIO_FPS}[{label}]'
            )
            filters.append(mix)

        add_mix(audio_labels, 'a_master')
        output_options.extend([
            '-map', '[a_master]', '-metadata:s:a:0', 'handler_name=Master',
            '-c:a', 'aac', '-b:a', '192k'
        ])
        if include_multitrack:
            ordered_groups = sorted(
                groups.items(), key=lambda item: group_labels[item[0]] != 'Лектор'
            )
            for track_index, (key, inputs) in enumerate(ordered_groups, start=1):
                label = f'a_track_{track_index}'
                add_mix(inputs, label)
                title = group_labels[key]
                if title == 'Аудио':
                    title = f'Аудио {track_index + 1}'
                output_options.extend([
                    '-map', f'[{label}]', '-metadata:s:a:' + str(track_index), f'handler_name={title}'
                ])
    else:
        output_options.append('-an')

    command.extend([
        '-filter_complex', ';'.join(filters),
        *output_options,
        '-c:v', video_encoder, *encoder_options, '-pix_fmt', 'yuv420p', '-r', str(TARGET_FPS),
        '-threads', str(os.cpu_count() or 1), '-nostats', '-progress', 'pipe:1',
        '-t', str(total_duration), output_path,
    ])
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    progress = tqdm.tqdm(
        total=total_duration,
        unit='s',
        desc='Rendering',
        unit_scale=True,
        # tqdm on Python 3.14 can expose a ``None`` value while it is closing
        # the bar. Avoid numeric format specifiers so the final display is
        # always safe even when one of the progress fields is unavailable.
        bar_format='{l_bar}{bar}| {n}/{total}s [{elapsed}<{remaining}, {rate_fmt}]',
    )
    last_time = 0.0
    for line in process.stdout or []:
        if line.startswith(('out_time_us=', 'out_time_ms=')):
            raw_value = line.split('=', 1)[1].strip()
            if raw_value == 'N/A':
                continue
            value = float(raw_value)
            current_time = value / 1_000_000
            progress.update(max(0.0, min(current_time, total_duration) - last_time))
            last_time = min(current_time, total_duration)
        elif line.strip() == 'progress=end':
            progress.update(max(0.0, total_duration - last_time))
    process.wait()
    progress.close()
    error_output = process.stderr.read() if process.stderr else ''
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, output=error_output)


def compile_final_video(total_duration: float, video_clips: List[VideoFileClip], audio_clips: List[AudioFileClip],
                        output_path: str, max_duration: Union[int, None], include_multitrack: bool = False):
    if max_duration:
        total_duration = min(total_duration, max_duration)

    try:
        compile_final_video_ffmpeg(
            total_duration, video_clips, audio_clips, output_path,
            include_multitrack=include_multitrack,
        )
        logging.info('Final video rendered with ffmpeg.')
        return
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        details = getattr(error, 'output', None)
        logging.warning('ffmpeg fast path failed (%s); falling back to MoviePy.', error)
        if details:
            logging.warning('ffmpeg fast path diagnostics: %s', str(details).strip())

    video_result = create_video_with_gaps(total_duration, video_clips)

    # MoviePy 2 first renders an attached AudioClip to a temporary file and
    # then invokes ffmpeg with ``-acodec copy``. Applying loudnorm in that
    # second invocation causes ffmpeg to fail because filters cannot be used
    # together with stream copy. Normalize the temporary audio while encoding
    # it, then mux the already-normalized file into the video unchanged.
    normalized_audio_path = None
    if audio_clips:
        combined_audio = create_audio_with_gaps(total_duration, audio_clips)
        normalized_audio_path = output_path + '.normalized.m4a'
        combined_audio.write_audiofile(
            normalized_audio_path,
            fps=TARGET_AUDIO_FPS,
            codec='aac',
            bitrate='192k',
            ffmpeg_params=['-af', AUDIO_LOUDNORM],
            logger=None,
        )

    # The MoviePy fallback concatenates decoded clips and can retain a longer
    # duration when source metadata contains overlapping or rotated streams.
    # Keep the fallback output on the same timeline as the ffmpeg path.
    if video_result.duration > total_duration:
        logging.info('Cropping MoviePy fallback to %.3f seconds.', total_duration)
        if hasattr(video_result, 'subclipped'):
            video_result = video_result.subclipped(0, total_duration)
        else:
            video_result = video_result.subclip(0, total_duration)

    try:
        video_result.write_videofile(
            output_path,
            codec='libx264',
            audio=normalized_audio_path or False,
            audio_codec='copy' if normalized_audio_path else None,
            fps=TARGET_FPS,
            preset='ultrafast',
            threads=os.cpu_count(),
            logger=None,
        )
    finally:
        if normalized_audio_path and os.path.exists(normalized_audio_path):
            os.remove(normalized_audio_path)
