import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Union

import httpx
import logging

TIMEOUT_SETTINGS = httpx.Timeout(None, connect=None)
DEFAULT_DOWNLOAD_WORKERS = 8


def construct_json_data_url(event_session_id: str, recording_id: str) -> str:
    if not event_session_id:
        raise ValueError('Missing webinar event session ID.')
    
    if not recording_id:
        return f'https://my.mts-link.ru/api/eventsessions/{event_session_id}/record?withoutCuts=false'
    return f'https://my.mts-link.ru/api/event-sessions/{event_session_id}/record-files/{recording_id}/flow?withoutCuts=false'


def fetch_json_data(url: str, session_id: Union[str, None]) -> Dict:
    cookies = {}
    if session_id:
        cookies['sessionId'] = session_id

    with httpx.Client(timeout=TIMEOUT_SETTINGS) as client:
        response = client.get(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0',
            },
            cookies=cookies
        )
        
    try:
        error_data = response.json()
        if error_data.get("error", {}).get("code") == 403:
            logging.error(
                'Access denied: session_id token is required. '
                'Provide it using the "--session-id" parameter.'
            )
            return
    except Exception:
        logging.warning('Server response does not contain JSON.')
            
    response.raise_for_status()
    return response.json()


def download_video_chunk(video_url: str, save_directory: str) -> str:
    filename = os.path.basename(video_url)
    file_path = os.path.join(save_directory, filename)

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        logging.info('Using existing fragment: %s', filename)
        return file_path

    logging.info('Downloading fragment: %s', filename)
    with open(file_path, 'wb') as file:
        with httpx.Client(timeout=TIMEOUT_SETTINGS) as client:
            with client.stream('GET', video_url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=8192):
                    file.write(chunk)
    logging.info('Downloaded fragment: %s (%.1f MB)', filename, os.path.getsize(file_path) / 1024**2)
    return file_path


def download_video_chunks(
    video_urls: Iterable[str],
    save_directory: str,
    max_workers: int = DEFAULT_DOWNLOAD_WORKERS,
) -> List[str]:
    """Download independent recording fragments concurrently.

    ``executor.map`` keeps the result order identical to ``video_urls``. This
    matters because the processor applies each downloaded file's event time
    after the downloads complete.
    """
    urls = list(video_urls)
    if not urls:
        return []
    if max_workers < 1:
        raise ValueError('max_workers must be at least 1.')

    worker_count = min(max_workers, len(urls))
    logging.info('Downloading %d fragments with %d parallel workers.', len(urls), worker_count)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix='mtslinker-download') as executor:
        return list(executor.map(lambda url: download_video_chunk(url, save_directory), urls))
