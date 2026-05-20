from mtslinker.timeline import StreamTimeline, MediaSession, TimeWindow, DownloadChunk


def _make_json(events, duration=100.0):
    return {'duration': duration, 'eventLogs': events}


def _ms_add(ms_id, url, time, conf_id=None, screenshare_id=None):
    data = {'id': ms_id, 'url': url}
    stream = {}
    if conf_id:
        stream['conference'] = {'id': conf_id}
    if screenshare_id:
        stream['screensharing'] = {'id': screenshare_id}
    if stream:
        data['stream'] = stream
    return {'module': 'mediasession.add', 'relativeTime': time, 'data': data}


def _ms_update(ms_id, duration):
    return {'module': 'mediasession.update', 'relativeTime': 0,
            'data': {'id': ms_id, 'duration': duration}}


def _conf_add(conf_id, user_id, has_audio=True, has_video=True):
    return {'module': 'conference.add', 'relativeTime': 0,
            'data': {'id': conf_id, 'user': {'id': user_id},
                     'hasAudio': has_audio, 'hasVideo': has_video}}


def test_single_session():
    json_data = _make_json([
        _ms_add('ms1', 'http://a.mp4', 0.0),
        _ms_update('ms1', 100.0),
    ])
    tl = StreamTimeline()
    windows = tl.build(json_data)
    assert len(windows) == 1
    assert windows[0].start_time == 0.0
    assert abs(windows[0].duration - 100.0) < 0.2
    assert len(windows[0].streams) == 1
    assert windows[0].streams[0].url == 'http://a.mp4'


def test_two_overlapping_sessions():
    json_data = _make_json([
        _ms_add('ms1', 'http://a.mp4', 0.0),
        _ms_update('ms1', 60.0),
        _ms_add('ms2', 'http://b.mp4', 10.0),
        _ms_update('ms2', 50.0),
    ], duration=60.0)
    tl = StreamTimeline()
    windows = tl.build(json_data)
    # Windows: [0-10] ms1 only, [10-60] ms1+ms2
    assert len(windows) == 2
    assert len(windows[0].streams) == 1
    assert len(windows[1].streams) == 2


def test_sequential_sessions():
    json_data = _make_json([
        _ms_add('ms1', 'http://a.mp4', 0.0),
        _ms_update('ms1', 30.0),
        _ms_add('ms2', 'http://b.mp4', 30.0),
        _ms_update('ms2', 30.0),
    ], duration=60.0)
    tl = StreamTimeline()
    windows = tl.build(json_data)
    assert len(windows) == 2
    assert len(windows[0].streams) == 1
    assert windows[0].streams[0].url == 'http://a.mp4'
    assert len(windows[1].streams) == 1
    assert windows[1].streams[0].url == 'http://b.mp4'


def test_gap_between_sessions():
    json_data = _make_json([
        _ms_add('ms1', 'http://a.mp4', 0.0),
        _ms_update('ms1', 20.0),
        _ms_add('ms2', 'http://b.mp4', 40.0),
        _ms_update('ms2', 20.0),
    ], duration=60.0)
    tl = StreamTimeline()
    windows = tl.build(json_data)
    # [0-20] ms1, [20-40] empty, [40-60] ms2
    assert len(windows) == 3
    assert len(windows[0].streams) == 1
    assert len(windows[1].streams) == 0
    assert len(windows[2].streams) == 1


def test_conference_audio_video_flags():
    json_data = _make_json([
        _conf_add('c1', 'u1', has_audio=True, has_video=False),
        _ms_add('ms1', 'http://a.mp4', 0.0, conf_id='c1'),
        _ms_update('ms1', 50.0),
    ], duration=50.0)
    tl = StreamTimeline()
    windows = tl.build(json_data)
    assert len(windows) == 1
    s = windows[0].streams[0]
    assert s.has_audio is True
    assert s.has_video is False


def test_admin_detection():
    json_data = _make_json([
        {'module': 'userlist.add', 'relativeTime': 0,
         'data': {'role': 'ADMIN', 'user': {'id': 'u1'}}},
        _conf_add('c1', 'u1'),
        _ms_add('ms1', 'http://a.mp4', 0.0, conf_id='c1'),
        _ms_update('ms1', 50.0),
    ], duration=50.0)
    tl = StreamTimeline()
    tl.build(json_data)
    assert 'c1' in tl.admin_conf_ids


def test_get_download_chunks():
    json_data = _make_json([
        _ms_add('ms1', 'http://a.mp4', 0.0, conf_id='c1'),
        _ms_update('ms1', 50.0),
        _ms_add('ms2', 'http://b.mp4', 10.0, conf_id='c2'),
        _ms_update('ms2', 40.0),
    ], duration=50.0)
    tl = StreamTimeline()
    tl.build(json_data)
    dl = tl.get_download_chunks()
    assert len(dl) == 2
    assert dl[0].url == 'http://a.mp4'
    assert dl[0].start_time == 0.0
    assert dl[1].url == 'http://b.mp4'


def test_map_downloaded_files():
    json_data = _make_json([
        _ms_add('ms1', 'http://a.mp4', 0.0, conf_id='c1'),
        _ms_update('ms1', 50.0),
    ], duration=50.0)
    tl = StreamTimeline()
    tl.build(json_data)
    tl.map_downloaded_files([('/tmp/a.mp4', 0.0, 'c1', False, 50.0)])
    assert tl.sessions['ms1'].file_path == '/tmp/a.mp4'


def test_three_webcam_overlap():
    """Three webcams with staggered starts — should produce multiple windows."""
    json_data = _make_json([
        _ms_add('ms1', 'http://a.mp4', 0.0),
        _ms_update('ms1', 100.0),
        _ms_add('ms2', 'http://b.mp4', 20.0),
        _ms_update('ms2', 80.0),
        _ms_add('ms3', 'http://c.mp4', 40.0),
        _ms_update('ms3', 60.0),
    ], duration=100.0)
    tl = StreamTimeline()
    windows = tl.build(json_data)
    # [0-20] 1 stream, [20-40] 2 streams, [40-100] 3 streams
    assert len(windows) == 3
    assert len(windows[0].streams) == 1
    assert len(windows[1].streams) == 2
    assert len(windows[2].streams) == 3


def test_empty_events():
    json_data = _make_json([])
    tl = StreamTimeline()
    windows = tl.build(json_data)
    assert windows == []


def test_fallback_url_capture():
    """URLs not in mediasession.add should be captured as fallback sessions."""
    json_data = _make_json([
        {'module': 'someother.event', 'relativeTime': 5.0,
         'data': {'url': 'http://fallback.mp4'}},
    ], duration=50.0)
    tl = StreamTimeline()
    windows = tl.build(json_data)
    assert len(tl.sessions) == 1
    assert any('fallback' in sid for sid in tl.sessions)


def test_screenshare_detection():
    """mediasession with stream.screensharing should be flagged."""
    json_data = _make_json([
        _ms_add('ms1', 'http://webcam.mp4', 0.0, conf_id='c1'),
        _ms_update('ms1', 50.0),
        _ms_add('ms2', 'http://screen.mp4', 10.0, screenshare_id=99),
        _ms_update('ms2', 40.0),
    ], duration=50.0)
    tl = StreamTimeline()
    tl.build(json_data)
    assert tl.sessions['ms1'].is_screenshare is False
    assert tl.sessions['ms2'].is_screenshare is True


def test_screenshare_not_detected_without_id():
    """Empty screensharing dict should not flag as screenshare."""
    json_data = _make_json([
        _ms_add('ms1', 'http://a.mp4', 0.0),
        _ms_update('ms1', 50.0),
    ], duration=50.0)
    tl = StreamTimeline()
    tl.build(json_data)
    assert tl.sessions['ms1'].is_screenshare is False


def test_time_window_properties():
    w = TimeWindow(start_time=10.0, duration=5.0, streams=[
        MediaSession(id='1', url='a', start_time=0, has_video=True, has_audio=False),
        MediaSession(id='2', url='b', start_time=0, has_video=False, has_audio=True),
    ])
    assert w.end_time == 15.0
    assert w.has_video is True
    assert w.has_audio is True
    assert w.stream_count == 2
