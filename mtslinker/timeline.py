"""StreamTimeline — builds a playback timeline from API mediasession events.

Replicates what the MTS-Link web player does: uses mediasession.add/update
events to know exactly which files to play and when, and conference events
to know which streams have audio/video.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class MediaSession:
    """A single media session (one recorded file)."""
    id: str
    url: str
    start_time: float
    duration: float = 0.0
    conf_id: Optional[str] = None
    has_audio: bool = True
    has_video: bool = True
    is_screenshare: bool = False
    user_id: Optional[str] = None
    file_path: Optional[str] = None  # set after download


@dataclass
class SlideEvent:
    """A presentation slide change event."""
    time: float
    slide_number: int
    slide_url: str
    local_path: Optional[str] = None  # set after download


@dataclass
class DownloadChunk:
    """A media chunk to download."""
    url: str
    start_time: float
    conf_id: Optional[str] = None
    is_admin: bool = False
    api_duration: float = 0.0
    session_id: Optional[str] = None


@dataclass
class GridSource:
    """A source stream for grid compositing."""
    path: str
    offset: float
    has_audio: bool = True
    is_admin: bool = False


@dataclass
class AudioTrack:
    """An audio track to merge into the final video."""
    path: str
    start_time: float
    origin: str = 'original'  # 'original' or 'webcam_extract'


@dataclass
class TimeWindow:
    """A time window where the set of active streams is constant."""
    start_time: float
    duration: float
    streams: List[MediaSession] = field(default_factory=list)

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration

    @property
    def has_video(self) -> bool:
        return any(s.has_video for s in self.streams)

    @property
    def has_audio(self) -> bool:
        return any(s.has_audio for s in self.streams)

    @property
    def stream_count(self) -> int:
        return len(self.streams)


class StreamTimeline:
    """Builds a timeline of active streams from API event logs.

    Mirrors the web player's approach: each mediasession gets an independent
    playback element, and all active streams play simultaneously.
    """

    def __init__(self):
        self.sessions: Dict[str, MediaSession] = {}
        self.admin_conf_ids: set = set()
        self.total_duration: float = 0.0
        self.slide_events: List[SlideEvent] = []

    def build(self, json_data: dict) -> List[TimeWindow]:
        """Parse API JSON and build timeline windows.

        Args:
            json_data: Full API response from /api/eventsessions/{sid}/record

        Returns:
            List of TimeWindow objects in chronological order.
        """
        self.total_duration = float(json_data.get('duration', 0))
        self._extract_admin_info(json_data)
        self._extract_sessions(json_data)
        self._extract_slides(json_data)
        windows = self._compute_windows()
        return windows

    def _extract_admin_info(self, json_data: dict):
        """Extract admin user IDs and their conference IDs."""
        admin_user_ids = set()
        user_to_conf = {}

        for event in json_data.get('eventLogs', []):
            if not isinstance(event, dict):
                continue
            module = event.get('module', '')
            data_list = event.get('data', [])
            if isinstance(data_list, dict):
                data_list = [data_list]
            if not isinstance(data_list, list):
                continue

            for d in data_list:
                if not isinstance(d, dict):
                    continue
                if 'userlist' in module:
                    role = d.get('role', '')
                    user = d.get('user', {})
                    if isinstance(user, dict) and role == 'ADMIN':
                        uid = user.get('id')
                        if uid:
                            admin_user_ids.add(uid)
                if module == 'conference.add':
                    user = d.get('user', {})
                    if isinstance(user, dict):
                        uid = user.get('id')
                        cid = d.get('id')
                        if uid and cid:
                            user_to_conf.setdefault(uid, set()).add(cid)

        for uid in admin_user_ids:
            self.admin_conf_ids.update(user_to_conf.get(uid, set()))

    def _extract_sessions(self, json_data: dict):
        """Extract mediasession.add/update events into MediaSession objects."""
        conf_users = {}
        conf_props = {}

        for event in json_data.get('eventLogs', []):
            if not isinstance(event, dict):
                continue
            module = event.get('module', '')
            data = event.get('data', {})
            if not isinstance(data, dict):
                continue

            if module == 'conference.add':
                cid = data.get('id')
                user = data.get('user', {})
                if isinstance(user, dict) and cid:
                    uid = user.get('id')
                    if uid:
                        conf_users[cid] = uid
                    conf_props[cid] = {
                        'has_audio': data.get('hasAudio', True),
                        'has_video': data.get('hasVideo', True),
                    }

            elif module == 'conference.update':
                cid = data.get('id')
                if cid and cid in conf_props:
                    if 'hasAudio' in data:
                        conf_props[cid]['has_audio'] = data['hasAudio']
                    if 'hasVideo' in data:
                        conf_props[cid]['has_video'] = data['hasVideo']

        for event in json_data.get('eventLogs', []):
            if not isinstance(event, dict):
                continue
            module = event.get('module', '')
            data = event.get('data', {})
            if not isinstance(data, dict):
                continue

            if module == 'mediasession.add' and 'url' in data:
                ms_id = data.get('id')
                if not ms_id:
                    continue
                conf_id = None
                is_screenshare = False
                stream = data.get('stream', {})
                if isinstance(stream, dict):
                    conf = stream.get('conference', {})
                    if isinstance(conf, dict):
                        conf_id = conf.get('id')
                    ss = stream.get('screensharing', {})
                    if isinstance(ss, dict) and ss.get('id'):
                        is_screenshare = True

                props = conf_props.get(conf_id, {})
                self.sessions[ms_id] = MediaSession(
                    id=ms_id,
                    url=data['url'],
                    start_time=event.get('relativeTime', 0),
                    conf_id=conf_id,
                    has_audio=props.get('has_audio', True),
                    has_video=props.get('has_video', True),
                    is_screenshare=is_screenshare,
                    user_id=conf_users.get(conf_id),
                )

            elif module == 'mediasession.update':
                ms_id = data.get('id')
                if ms_id and ms_id in self.sessions:
                    if 'duration' in data:
                        self.sessions[ms_id].duration = float(data['duration'])

        # Fallback: pick up URLs not covered by mediasession events
        known_urls = {s.url for s in self.sessions.values()}
        for event in json_data.get('eventLogs', []):
            if not isinstance(event, dict):
                continue
            module = event.get('module', '')
            data = event.get('data', {})
            if not isinstance(data, dict):
                continue
            if module == 'mediasession.add':
                continue
            url = data.get('url')
            if url and url not in known_urls:
                fallback_id = f'fallback_{len(self.sessions)}'
                conf_id = None
                stream = data.get('stream', {})
                if isinstance(stream, dict):
                    conf = stream.get('conference', {})
                    if isinstance(conf, dict):
                        conf_id = conf.get('id')
                self.sessions[fallback_id] = MediaSession(
                    id=fallback_id,
                    url=url,
                    start_time=event.get('relativeTime', 0),
                    conf_id=conf_id,
                )
                known_urls.add(url)

        logging.info(f'Timeline: {len(self.sessions)} media sessions extracted')

    def _extract_slides(self, json_data: dict):
        """Extract presentation.update events into SlideEvent objects."""
        raw_slides = []
        for event in json_data.get('eventLogs', []):
            if not isinstance(event, dict):
                continue
            if event.get('module') != 'presentation.update':
                continue
            data = event.get('data', {})
            if not isinstance(data, dict):
                continue
            fr = data.get('fileReference', {})
            if not isinstance(fr, dict):
                continue
            slide = fr.get('slide', {})
            if not isinstance(slide, dict) or not slide.get('url'):
                continue
            raw_slides.append(SlideEvent(
                time=event.get('relativeTime', 0),
                slide_number=slide.get('number', 0),
                slide_url=slide['url'],
            ))

        # Deduplicate consecutive identical slides
        self.slide_events = []
        for se in raw_slides:
            if not self.slide_events or se.slide_url != self.slide_events[-1].slide_url:
                self.slide_events.append(se)

        if self.slide_events:
            logging.info(f'Timeline: {len(self.slide_events)} presentation slide changes')

    def _compute_windows(self) -> List[TimeWindow]:
        """Compute time windows where the set of active streams is constant."""
        if not self.sessions:
            return []

        boundaries = set()
        boundaries.add(0.0)
        if self.total_duration > 0:
            boundaries.add(self.total_duration)

        for s in self.sessions.values():
            boundaries.add(s.start_time)
            end = s.start_time + s.duration if s.duration > 0 else self.total_duration
            boundaries.add(end)

        sorted_times = sorted(boundaries)

        windows = []
        prev_streams_key = None
        merge_start = None
        merge_streams = None

        for i in range(len(sorted_times) - 1):
            t_start = sorted_times[i]
            t_end = sorted_times[i + 1]
            if t_end - t_start < 0.1:
                continue

            active = []
            for s in self.sessions.values():
                s_end = s.start_time + s.duration if s.duration > 0 else self.total_duration
                if s.start_time < t_end and s_end > t_start:
                    active.append(s)

            streams_key = tuple(s.id for s in sorted(active, key=lambda x: x.id))

            if streams_key == prev_streams_key and merge_start is not None:
                continue
            else:
                if merge_start is not None:
                    windows.append(TimeWindow(
                        start_time=merge_start,
                        duration=t_start - merge_start,
                        streams=merge_streams,
                    ))
                merge_start = t_start
                merge_streams = active
                prev_streams_key = streams_key

        if merge_start is not None and sorted_times:
            windows.append(TimeWindow(
                start_time=merge_start,
                duration=sorted_times[-1] - merge_start,
                streams=merge_streams,
            ))

        windows = [w for w in windows if w.duration > 0.1]

        logging.info(f'Timeline: {len(windows)} time windows computed')
        return windows

    def get_download_chunks(self) -> List[DownloadChunk]:
        """Get list of chunks to download as DownloadChunk objects."""
        result = []
        for s in self.sessions.values():
            result.append(DownloadChunk(
                url=s.url,
                start_time=s.start_time,
                conf_id=s.conf_id,
                is_admin=s.conf_id in self.admin_conf_ids,
                api_duration=s.duration,
                session_id=s.id,
            ))
        return sorted(result, key=lambda x: x.start_time)

    def get_slide_events_as_dicts(self) -> List[dict]:
        """Get slide events as dicts (compatible with existing download pipeline)."""
        return [
            {'time': se.time, 'slide_number': se.slide_number,
             'slide_url': se.slide_url}
            for se in self.slide_events
        ]

    def map_downloaded_files(self, downloaded_files: list):
        """Map downloaded files back to sessions.

        Args:
            downloaded_files: List of DownloadedFile-like objects or
                (path, start_time, conf_id, is_admin) tuples.
        """
        time_conf_to_session = {}
        for s in self.sessions.values():
            key = (round(s.start_time, 1), s.conf_id)
            time_conf_to_session[key] = s

        for item in downloaded_files:
            if hasattr(item, 'path'):
                path, start_time, conf_id = item.path, item.start_time, item.conf_id
            else:
                path = item[0]
                start_time = item[1]
                conf_id = item[2] if len(item) > 2 else None

            key = (round(start_time, 1), conf_id)
            session = time_conf_to_session.get(key)
            if session:
                session.file_path = path
            else:
                for s in self.sessions.values():
                    if abs(s.start_time - start_time) < 0.5 and s.file_path is None:
                        s.file_path = path
                        break

    def get_windows_with_files(self) -> List[TimeWindow]:
        """Get windows filtered to only include streams with downloaded files."""
        result = []
        for w in self._compute_windows():
            filed_streams = [s for s in w.streams if s.file_path]
            if filed_streams:
                result.append(TimeWindow(
                    start_time=w.start_time,
                    duration=w.duration,
                    streams=filed_streams,
                ))
        return result
