import logging
import os
import re

from mtslinker.downloader import construct_json_data_url, fetch_json_data
from mtslinker.processor import compile_final_video, process_video_clips
from mtslinker.utils import create_directory_if_not_exists


def _configure_recording_logging(directory):
    root_logger = logging.getLogger()
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    )
    log_path = os.path.abspath(os.path.join(directory, 'recording.log'))
    if not any(
        isinstance(handler, logging.FileHandler)
        and os.path.abspath(handler.baseFilename) == log_path
        for handler in root_logger.handlers
    ):
        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.info('Recording log: %s', log_path)


def fetch_webinar_data(
    event_sessions: str,
    record_id: str,
    session_id=None,
    max_duration=None,
    download_workers=8,
    include_multitrack=False,
):
    json_data_url = construct_json_data_url(event_session_id=event_sessions, recording_id=record_id)
    json_data = fetch_json_data(url=json_data_url, session_id=session_id)
    
    if not json_data:
        logging.error('Failed to fetch webinar data. Check the session ID or URL.')
        return

    sanitized_name = re.sub(r'[\s\/:*?"<>|]+', '_', json_data['name'])
    directory = create_directory_if_not_exists(sanitized_name)
    _configure_recording_logging(directory)
    output_video_path = os.path.join(directory, f'{sanitized_name}.mp4')

    total_duration, video_clips, audio_clips = process_video_clips(
        directory, json_data, download_workers=download_workers
    )
    logging.info(
        f'Downloaded and processed {len(video_clips) + len(audio_clips)} files ({total_duration} sec) for merging.')

    compile_final_video(
        total_duration, video_clips, audio_clips, output_video_path, max_duration,
        include_multitrack=include_multitrack,
    )
    logging.info(f'Final video saved to {output_video_path}')
    
    return 1
