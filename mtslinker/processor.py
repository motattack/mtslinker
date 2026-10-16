import logging
import os
from typing import Dict, Tuple, List, Union

import numpy as np
from moviepy.audio.AudioClip import AudioArrayClip, CompositeAudioClip
from moviepy.audio.io.AudioFileClip import AudioFileClip
from moviepy.video.VideoClip import ColorClip
from moviepy import VideoFileClip, concatenate_videoclips

import warnings
warnings.simplefilter("ignore")


from mtslinker.downloader import download_video_chunk


def process_video_clips(directory: str, json_data: Dict) -> Tuple[float, List[VideoFileClip], List[AudioFileClip]]:
    total_duration = float(json_data.get('duration', 0))
    if not total_duration:
        raise ValueError('Duration not found in JSON data.')

    video_clips = []
    audio_clips = []

    for event in json_data.get('eventLogs', []):
        if isinstance(event, dict):
            data = event.get('data', {})
            if isinstance(data, dict) and 'url' in data:
                url = data['url']
                start_time = event.get('relativeTime', 0)

                downloaded_file_path = download_video_chunk(url, directory)
                try:
                    video_clip = VideoFileClip(downloaded_file_path, fps_source='fps').with_start(start_time)
                    video_clips.append(video_clip)
                except (KeyError, OSError):
                    audio_clip = AudioFileClip(downloaded_file_path).with_start(start_time)
                    audio_clips.append(audio_clip)
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

    final_video = concatenate_videoclips(clips, method='compose')
    logging.info(f'Final video duration: {final_video.duration}')
    return final_video


def create_audio_with_gaps(total_duration: float, audio_clips: List[AudioFileClip]) -> CompositeAudioClip:
    audio_segments = []
    current_time = 0

    for audio in audio_clips:
        if audio.start > current_time:
            gap_duration = audio.start - current_time
            if gap_duration > 0:
                # A positive gap can round down to zero samples.
                silence_segment = AudioArrayClip(np.zeros((max(1, int(gap_duration * 8000)), 2)), fps=8000).with_start(
                    current_time)
                audio_segments.append(silence_segment)

        audio_segments.append(audio)
        current_time = audio.end

    if current_time < total_duration:
        remaining_duration = total_duration - current_time
        silence_segment = AudioArrayClip(np.zeros((max(1, int(remaining_duration * 8000)), 2)), fps=8000).with_start(
            current_time)
        audio_segments.append(silence_segment)

    final_audio = CompositeAudioClip(audio_segments)
    logging.info(f'Total audio duration: {final_audio.duration}')
    return final_audio


def compile_final_video(total_duration: float, video_clips: List[VideoFileClip], audio_clips: List[AudioFileClip],
                        output_path: str, max_duration: Union[int, None]):
    video_result = create_video_with_gaps(total_duration, video_clips)

    if audio_clips:
        combined_audio = create_audio_with_gaps(total_duration, audio_clips)
        video_result = video_result.with_audio(combined_audio)

    if max_duration:
        if video_result.duration > max_duration:
            logging.info(f'Duration limit! Crop!')
            video_result = video_result.subclip(0, max_duration)

    video_result.write_videofile(
        output_path,
        codec='libx264',
        audio_codec='aac',
        preset='ultrafast',
        threads=os.cpu_count()
    )
