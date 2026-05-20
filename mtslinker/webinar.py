import logging
import os
import re

from mtslinker.downloader import (
    construct_json_data_url,
    fetch_json_data,
    download_chunks_parallel,
    download_slide_images,
)
from mtslinker.processor import compile_final_video, process_and_download_clips
from mtslinker.utils import create_directory_if_not_exists


def fetch_webinar_data(event_sessions: str, record_id: str, session_id=None, max_duration=None):
    json_data_url = construct_json_data_url(event_session_id=event_sessions, recording_id=record_id)
    json_data = fetch_json_data(url=json_data_url, session_id=session_id)

    if not json_data:
        logging.error('Failed to fetch webinar data. Check the session ID or URL.')
        return

    sanitized_name = re.sub(r'[\s\/:*?"<>|]+', '_', json_data['name'])
    directory = create_directory_if_not_exists(sanitized_name)
    output_video_path = os.path.join(directory, f'{sanitized_name}.mp4')

    total_duration, chunks, slide_events, timeline = process_and_download_clips(directory, json_data)
    logging.info(f'Found {len(chunks)} chunks to download ({total_duration} sec total)')

    # Download all chunks in parallel
    downloaded_files = download_chunks_parallel(chunks, directory)
    logging.info(f'Downloaded {len(downloaded_files)} files, starting merge...')

    # Download presentation slides if any
    downloaded_slides = []
    if slide_events:
        downloaded_slides = download_slide_images(slide_events, directory)

    compile_final_video(
        total_duration, downloaded_files, directory, output_video_path,
        max_duration, slide_events=downloaded_slides, timeline=timeline,
    )
    logging.info(f'Final video saved to {output_video_path}')

    return 1
