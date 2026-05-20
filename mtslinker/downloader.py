import os
import subprocess
from typing import Dict, List, Tuple, Union
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import tqdm
import logging

TIMEOUT_SETTINGS = httpx.Timeout(None, connect=None)
CHUNK_SIZE = 1024 * 1024  # 1 MB
MAX_PARALLEL_DOWNLOADS = 4


def _storage_to_hls_url(storage_url: str) -> str:
    """Convert events-storage URL to events-delivery-records HLS URL."""
    return (
        storage_url
        .replace('events-storage.webinar.ru/api-storage/files/wowza/',
                 'events-delivery-records.webinar.ru/record/')
        + '/playlist.m3u8'
    )


def _hls_is_available(hls_url: str) -> bool:
    """Check if an HLS playlist is reachable and non-empty."""
    try:
        with httpx.Client(timeout=httpx.Timeout(10)) as client:
            r = client.get(hls_url, headers={'User-Agent': 'Mozilla/5.0'})
            return r.status_code == 200 and '#EXTM3U' in r.text
    except Exception:
        return False


def download_hls_chunk(hls_url: str, save_path: str) -> str:
    """Download an HLS stream (video, audio, or both) to mp4 using ffmpeg."""
    if not os.path.exists(save_path):
        subprocess.run(
            [
                'ffmpeg', '-y', '-v', 'warning',
                '-i', hls_url,
                '-c', 'copy',
                save_path,
            ],
            check=True,
        )
    return save_path


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


def _validate_downloaded_file(file_path: str) -> bool:
    """Quick check that a downloaded media file is not corrupt."""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', file_path],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def download_video_chunk(video_url: str, save_directory: str, max_retries: int = 2) -> str:
    filename = os.path.basename(video_url)
    file_path = os.path.join(save_directory, filename)

    if os.path.exists(file_path):
        if _validate_downloaded_file(file_path):
            return file_path
        logging.warning(f'Existing file corrupt, re-downloading: {filename}')
        os.remove(file_path)

    for attempt in range(max_retries + 1):
        hls_url = _storage_to_hls_url(video_url)
        hls_ok = _hls_is_available(hls_url)
        if hls_ok:
            logging.info(f'Downloading via HLS: {filename}')
            try:
                download_hls_chunk(hls_url, file_path)
            except subprocess.CalledProcessError:
                logging.warning(f'HLS download failed for {filename}, falling back to direct')
                if os.path.exists(file_path):
                    os.remove(file_path)
                hls_ok = False
        if not hls_ok:
            logging.info(f'Downloading direct: {filename}')
            with open(file_path, 'wb') as file:
                with httpx.Client(timeout=TIMEOUT_SETTINGS) as client:
                    with client.stream('GET', video_url) as response:
                        response.raise_for_status()
                        total_size = int(response.headers.get('content-length', 0))
                        with tqdm.tqdm(total=total_size, unit='B', unit_scale=True,
                                       desc=f'Downloading {filename}') as progress:
                            for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                                file.write(chunk)
                                progress.update(len(chunk))

        if _validate_downloaded_file(file_path):
            return file_path

        if attempt < max_retries:
            logging.warning(f'Downloaded file corrupt (attempt {attempt+1}), retrying: {filename}')
            os.remove(file_path)
        else:
            logging.error(f'File still corrupt after {max_retries+1} attempts: {filename}')

    return file_path


def download_slide_images(
    slide_events: List[Dict],
    save_directory: str,
) -> List[Dict]:
    """Download slide images and add local_path to each event.

    Deduplicates by URL so each unique slide is downloaded once.
    """
    slides_dir = os.path.join(save_directory, 'slides')
    os.makedirs(slides_dir, exist_ok=True)

    url_to_path = {}
    for se in slide_events:
        url = se['slide_url']
        if url in url_to_path:
            continue
        local_path = os.path.join(slides_dir, f"slide_{se['slide_number']}.jpg")
        if not os.path.exists(local_path):
            try:
                with httpx.Client(timeout=httpx.Timeout(30)) as client:
                    r = client.get(url)
                    r.raise_for_status()
                    with open(local_path, 'wb') as f:
                        f.write(r.content)
            except Exception as e:
                logging.warning(f"Failed to download slide {se['slide_number']}: {e}")
                continue
        url_to_path[url] = local_path

    result = []
    for se in slide_events:
        path = url_to_path.get(se['slide_url'])
        if path:
            result.append({**se, 'local_path': path})

    logging.info(f'Downloaded {len(url_to_path)} unique slide images')
    return result


def download_chunks_parallel(
    chunks: list,
    save_directory: str,
    max_workers: int = MAX_PARALLEL_DOWNLOADS,
) -> list:
    """Download multiple chunks in parallel.

    Args:
        chunks: List of (url, start_time[, conf_id[, is_admin]]) tuples.
        save_directory: Directory to save files to.
        max_workers: Maximum number of parallel downloads.

    Returns:
        List of (file_path, start_time, conf_id, is_admin) tuples in original order.
    """
    results = [None] * len(chunks)

    def _download(index, url, start_time, conf_id, is_admin):
        path = download_video_chunk(url, save_directory)
        return index, path, start_time, conf_id, is_admin

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for i, chunk in enumerate(chunks):
            url, start_time = chunk[0], chunk[1]
            conf_id = chunk[2] if len(chunk) > 2 else None
            is_admin = chunk[3] if len(chunk) > 3 else False
            futures.append(
                executor.submit(_download, i, url, start_time, conf_id, is_admin)
            )
        for future in as_completed(futures):
            idx, path, start_time, conf_id, is_admin = future.result()
            results[idx] = (path, start_time, conf_id, is_admin)

    return results
