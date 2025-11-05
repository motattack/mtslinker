import os
from typing import Dict, Union

import httpx
import tqdm
import logging

TIMEOUT_SETTINGS = httpx.Timeout(None, connect=None)


def construct_json_data_url(event_session_id: str, recording_id: str) -> str:
    if not event_session_id:
        raise ValueError('Missing webinar event session ID.')
    
    if not recording_id:
        return f'https://my.mts-link.ru/api/eventsessions/{event_session_id}/record?withoutCuts=false'
    return f'https://my.mts-link.ru/api/event-sessions/{event_session_id}/record-files/{recording_id}/flow?withoutCuts=false'


def fetch_json_data(url: str, session_id: Union[str, None]) -> Union[Dict, None]:
    cookies = {}
    if session_id:
        cookies['sessionId'] = session_id

    try:
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
                return None
        except Exception:
            pass
                
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f'Failed to fetch JSON data: {e}')
        return None


def download_video_chunk(video_url: str, save_directory: str) -> Union[str, None]:
    try:
        filename = os.path.basename(video_url)
        file_path = os.path.join(save_directory, filename)

        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            logging.info(f'File already exists: {filename}')
            return file_path

        with open(file_path, 'wb') as file:
            with httpx.Client(timeout=TIMEOUT_SETTINGS) as client:
                with client.stream('GET', video_url) as response:
                    response.raise_for_status()
                    total_size = int(response.headers.get('content-length', 0))
                    downloaded = 0
                    with tqdm.tqdm(total=total_size, unit='B', unit_scale=True,
                                   desc=f'Downloading {filename}') as progress:
                        for chunk in response.iter_bytes(chunk_size=8192):
                            if chunk:
                                file.write(chunk)
                                downloaded += len(chunk)
                                progress.update(len(chunk))
        
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return file_path
        else:
            logging.error(f'Downloaded file is empty: {file_path}')
            return None
            
    except Exception as e:
        logging.error(f'Failed to download {video_url}: {e}')
        return None
