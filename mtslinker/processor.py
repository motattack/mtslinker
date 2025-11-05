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


def process_video_clips(directory: str, json_data: Dict) -> Tuple[float, List[Tuple[float, VideoFileClip]], List[Tuple[float, AudioFileClip]]]:
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
                start_time = float(event.get('relativeTime', 0))

                downloaded_file_path = download_video_chunk(url, directory)
                if not downloaded_file_path or not os.path.exists(downloaded_file_path):
                    logging.warning(f'Failed to download: {url}')
                    continue
                
                try:
                    video_clip = VideoFileClip(downloaded_file_path)
                    video_clips.append((start_time, video_clip))
                except (KeyError, OSError, Exception) as e:
                    logging.warning(f'Failed to load video {downloaded_file_path}, trying audio: {e}')
                    try:
                        audio_clip = AudioFileClip(downloaded_file_path)
                        audio_clips.append((start_time, audio_clip))
                    except Exception as audio_e:
                        logging.error(f'Failed to load audio {downloaded_file_path}: {audio_e}')
    
    logging.info(f'Total duration of clips: {total_duration}')
    logging.info(f'Loaded {len(video_clips)} video clips and {len(audio_clips)} audio clips')

    return total_duration, video_clips, audio_clips


def create_video_with_gaps(total_duration: float, video_clips: List[Tuple[float, VideoFileClip]]) -> VideoFileClip:
    if not video_clips:
        empty_clip = ColorClip(size=(1920, 1080), color=(0, 0, 0), duration=total_duration)
        return empty_clip
    
    video_clips_sorted = sorted(video_clips, key=lambda x: x[0])
    clips = []
    current_time = 0.0
    
    width = video_clips_sorted[0][1].w
    height = video_clips_sorted[0][1].h

    for start_time, video in video_clips_sorted:
        if start_time > current_time:
            gap_duration = start_time - current_time
            if gap_duration > 0.01:
                empty_clip = ColorClip(size=(width, height), color=(0, 0, 0), duration=gap_duration)
                clips.append(empty_clip)
                current_time += gap_duration

        clips.append(video)
        current_time += video.duration

    if current_time < total_duration:
        remaining_duration = total_duration - current_time
        if remaining_duration > 0.01:
            empty_clip = ColorClip(size=(width, height), color=(0, 0, 0), duration=remaining_duration)
            clips.append(empty_clip)

    final_video = concatenate_videoclips(clips, method='chain')
    logging.info(f'Final video duration: {final_video.duration}')
    return final_video


def create_audio_with_gaps(total_duration: float, audio_clips: List[Tuple[float, AudioFileClip]]) -> CompositeAudioClip:
    if not audio_clips:
        silence = AudioArrayClip(np.zeros((int(total_duration * 44100), 2)), fps=44100)
        return silence
    
    audio_clips_sorted = sorted(audio_clips, key=lambda x: x[0])
    audio_segments = []

    for start_time, audio in audio_clips_sorted:
        audio_with_start = audio.with_start(start_time)
        audio_segments.append(audio_with_start)

    final_audio = CompositeAudioClip(audio_segments)
    
    if final_audio.duration < total_duration:
        silence_duration = total_duration - final_audio.duration
        if silence_duration > 0.01:
            silence = AudioArrayClip(np.zeros((int(silence_duration * 44100), 2)), fps=44100).with_start(final_audio.duration)
            audio_segments.append(silence)
            final_audio = CompositeAudioClip(audio_segments)
    
    logging.info(f'Total audio duration: {final_audio.duration}')
    return final_audio


def compile_final_video(total_duration: float, video_clips: List[Tuple[float, VideoFileClip]], audio_clips: List[Tuple[float, AudioFileClip]],
                        output_path: str, max_duration: Union[int, None]):
    try:
        video_result = create_video_with_gaps(total_duration, video_clips)

        if audio_clips:
            combined_audio = create_audio_with_gaps(total_duration, audio_clips)
            video_result = video_result.with_audio(combined_audio)

        if max_duration:
            if video_result.duration > max_duration:
                logging.info(f'Duration limit! Crop to {max_duration} seconds')
                video_result = video_result.subclip(0, max_duration)

        logging.info(f'Writing final video to {output_path}')
        video_result.write_videofile(
            output_path,
            codec='libx264',
            audio_codec='aac',
            preset='medium',
            threads=os.cpu_count(),
            fps=24,
            bitrate='5000k',
            audio_bitrate='192k'
        )
        
        video_result.close()
        for _, clip in video_clips:
            clip.close()
        for _, clip in audio_clips:
            clip.close()
            
    except Exception as e:
        logging.error(f'Failed to compile final video: {e}')
        raise
