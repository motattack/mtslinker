"""Video processor — two-phase pipeline: analyze then execute.

Uses composition of specialized classes:
  FFmpegRunner  — ffmpeg execution, GPU detection
  MediaProber   — file probing, duration, streams
  SegmentBuilder — normalize, gaps, dedup
  GridCompositor — multi-webcam grid layout
  SlideCompositor — presentation slide overlay
  AudioMerger   — batched audio mixing
"""
import json
import logging
import os
import shutil
import subprocess
from typing import List

from mtslinker.ffmpeg import FFmpegRunner
from mtslinker.prober import MediaProber
from mtslinker.segments import SegmentBuilder
from mtslinker.grid import GridCompositor
from mtslinker.slides import SlideCompositor
from mtslinker.audio import AudioMerger
from mtslinker.timeline import StreamTimeline, GridSource, AudioTrack


class VideoProcessor:
    """Orchestrates the two-phase video processing pipeline."""

    def __init__(self):
        self.ffmpeg = FFmpegRunner()
        self.prober = MediaProber()
        self.segments = SegmentBuilder(self.ffmpeg, self.prober)
        self.grid = GridCompositor(self.ffmpeg)
        self.slides = SlideCompositor(self.ffmpeg)
        self.audio = AudioMerger(self.ffmpeg, self.prober)

    def analyze(self, total_duration, downloaded_files, directory,
                output_path, max_duration=None, slide_events=None,
                timeline=None):
        """Phase 1: Probe all files, make all decisions, write manifest.

        Uses the StreamTimeline built from mediasession events as the source
        of truth — matches what the JS player does.
        """
        logging.info('Phase 1: Analyzing files...')

        all_files = self.prober.probe_all_files(downloaded_files)

        video_files = []
        audio_files = []
        skipped = 0
        errors = []

        for f in all_files:
            if not f['valid']:
                skipped += 1
                errors.append(f'Corrupt/invalid: {f["path"]}')
                continue
            if f['has_video']:
                video_files.append(f)
            else:
                audio_files.append(f)

        if skipped:
            logging.warning(f'Skipped {skipped} corrupt/invalid files')
        logging.info(f'Segments: {len(video_files)} video, {len(audio_files)} audio-only')

        if not video_files:
            raise ValueError('No valid video segments found.')

        # Determine target resolution
        target_w, target_h, target_pix_fmt = 0, 0, 'yuv420p'
        for f in video_files:
            if f['width'] * f['height'] > target_w * target_h:
                target_w, target_h, target_pix_fmt = f['width'], f['height'], f['pix_fmt']
        if target_w * target_h < 640 * 360:
            target_w, target_h = 640, 360
        MAX_H = 720
        if target_h > MAX_H:
            target_w = int(target_w * MAX_H / target_h)
            target_w -= target_w % 2
            target_h = MAX_H
        logging.info(f'Target resolution: {target_w}x{target_h}, pix_fmt={target_pix_fmt}')

        # Build segment plan from timeline (mediasession events).
        # Each stream gets an independent playback element, all active
        # streams play simultaneously — no dedup/overlap guessing needed.
        segments = []
        timeline.map_downloaded_files(downloaded_files)
        tw_windows = timeline.get_windows_with_files()
        path_lookup = {f['path']: f for f in video_files}

        for tw in tw_windows:
            sources = []
            for stream in tw.streams:
                if not stream.file_path or stream.file_path not in path_lookup:
                    continue
                offset = max(0, tw.start_time - stream.start_time)
                f = path_lookup[stream.file_path]
                is_admin = stream.conf_id in timeline.admin_conf_ids
                sources.append({
                    'path': stream.file_path,
                    'offset': offset,
                    'remaining': tw.duration,
                    'has_audio': stream.has_audio and f['has_audio'],
                    'is_admin': is_admin,
                    'is_screenshare': stream.is_screenshare,
                })
            if not sources:
                segments.append({
                    'type': 'gap',
                    'duration': tw.duration,
                    'start_time': tw.start_time,
                })
            elif len(sources) == 1:
                s = sources[0]
                f = path_lookup[s['path']]
                segments.append({
                    'type': 'video',
                    'source_path': s['path'],
                    'start_time': tw.start_time,
                    'source_offset': s['offset'],
                    'source_duration': f['duration'],
                    'planned_duration': tw.duration,
                    'has_audio': s['has_audio'],
                    'width': f['width'],
                    'height': f['height'],
                })
            else:
                # Presenter layout: admin/screenshare as main, others as PIP
                screenshares = [s for s in sources if s.get('is_screenshare')]
                admins = [s for s in sources if s.get('is_admin') and not s.get('is_screenshare')]
                others = [s for s in sources if not s.get('is_admin') and not s.get('is_screenshare')]

                main_source = None
                overlay_source = None
                if screenshares:
                    main_source = screenshares[0]
                    overlay_source = admins[0] if admins else (others[0] if others else None)
                elif admins:
                    main_source = admins[0]
                    overlay_source = others[0] if others else None

                if main_source:
                    used = {id(main_source)}
                    if overlay_source:
                        used.add(id(overlay_source))
                    extra_audio = [s for s in sources if id(s) not in used and s.get('has_audio')]
                    segments.append({
                        'type': 'presenter',
                        'start_time': tw.start_time,
                        'planned_duration': tw.duration,
                        'main_source': main_source,
                        'overlay_source': overlay_source,
                        'extra_audio_sources': extra_audio,
                        'mix_all_audio': True,
                    })
                else:
                    # No admin, no screenshare — fall back to grid
                    segments.append({
                        'type': 'grid',
                        'start_time': tw.start_time,
                        'planned_duration': tw.duration,
                        'sources': sources,
                        'mix_all_audio': True,
                    })

        # Fill leading/trailing gaps
        filled = []
        current_time = 0.0
        for s in segments:
            start = s.get('start_time', 0)
            if start - current_time > 0.1:
                filled.append({
                    'type': 'gap',
                    'duration': start - current_time,
                    'start_time': current_time,
                })
            filled.append(s)
            current_time = start + s.get('planned_duration', s.get('duration', 0))
        if current_time < total_duration - 0.1:
            filled.append({
                'type': 'gap',
                'duration': total_duration - current_time,
                'start_time': current_time,
            })
        segments = filled

        logging.info(f'Timeline: {len(segments)} segments from mediasession events')

        # Audio tracks — only audio-only files (webcam audio is mixed
        # inline by GridCompositor, no separate extraction needed)
        all_audio = [{'path': f['path'], 'start_time': f['start_time'],
                      'origin': 'original'} for f in audio_files]

        # Slide compositing plan
        slide_plan = None
        if slide_events:
            CHUNK_SECS = 1800
            n_chunks = max(1, int(total_duration + CHUNK_SECS - 1) // CHUNK_SECS)
            slide_plan = {
                'events': slide_events,
                'chunk_seconds': CHUNK_SECS,
                'num_chunks': n_chunks,
            }

        # GPU detection
        self.ffmpeg.detect_gpu()
        gpu = {
            'nvenc': bool(self.ffmpeg.nvenc_available),
            'cuda_overlay': bool(self.ffmpeg.cuda_overlay_available),
        }

        # Validation warnings
        warnings = []
        for seg in segments:
            if seg['type'] != 'video':
                continue
            src_dur = seg.get('source_duration', 0)
            planned = seg.get('planned_duration', 0)
            if src_dur and planned and src_dur < planned - 1.0:
                warnings.append(
                    f'Segment at {seg["start_time"]:.0f}s: source={src_dur:.0f}s '
                    f'but planned={planned:.0f}s (will pad {planned-src_dur:.0f}s black)'
                )

        manifest = {
            'version': 1,
            'total_duration': total_duration,
            'directory': directory,
            'output_path': output_path,
            'max_duration': max_duration,
            'target': {'width': target_w, 'height': target_h, 'pix_fmt': target_pix_fmt},
            'overlap_strategy': 'timeline',
            'segments': segments,
            'audio_tracks': all_audio,
            'slide_compositing': slide_plan,
            'gpu': gpu,
            'errors': errors,
            'warnings': warnings,
            'stats': {
                'total_files': len(all_files),
                'video_files': len(video_files),
                'audio_files': len(audio_files),
                'kept_video': 0,
                'extra_audio': 0,
                'skipped': skipped,
                'segments': len(segments),
                'gaps': sum(1 for s in segments if s['type'] == 'gap'),
            },
        }

        manifest_path = os.path.join(directory, 'manifest.json')
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        logging.info(f'Manifest written to {manifest_path}')
        logging.info(f'Plan: {manifest["stats"]["segments"]} segments '
                     f'({manifest["stats"]["gaps"]} gaps), '
                     f'{len(all_audio)} audio tracks, '
                     f'strategy=timeline')

        if errors:
            for e in errors:
                logging.warning(f'Analyze: {e}')
        if warnings:
            for w in warnings:
                logging.warning(f'Analyze: {w}')

        return manifest

    def execute(self, manifest):
        """Phase 2: Execute the plan from the manifest."""
        logging.info('Phase 2: Executing plan...')

        directory = manifest['directory']
        output_path = manifest['output_path']
        total_duration = manifest['total_duration']
        target = manifest['target']
        target_w, target_h = target['width'], target['height']
        target_pix_fmt = target['pix_fmt']

        tmp_dir = os.path.join(directory, '_tmp_ffmpeg')
        os.makedirs(tmp_dir, exist_ok=True)

        # Collect audio-only tracks (webcam audio is mixed inline by grid)
        audio_files = [
            AudioTrack(path=t['path'], start_time=t['start_time'], origin=t['origin'])
            for t in manifest['audio_tracks']
        ]

        # Process segments
        segments = manifest['segments']
        grid_dir = os.path.join(tmp_dir, 'grid_segments')
        concat_segments = []

        for i, seg in enumerate(segments):
            if seg['type'] == 'gap':
                gap_path = os.path.join(tmp_dir, f'gap_{i}.mp4')
                self.segments.generate_black(gap_path, seg['duration'],
                                             target_w, target_h, target_pix_fmt)
                concat_segments.append(gap_path)
                logging.info(f'Generated {seg["duration"]:.1f}s black gap')

            elif seg['type'] == 'grid':
                os.makedirs(grid_dir, exist_ok=True)
                seg_path = os.path.join(grid_dir, f'grid_{i}.mp4')
                duration = seg['planned_duration']
                sources = sorted(seg['sources'],
                                 key=lambda s: not s.get('has_audio', False))
                mix_audio = seg.get('mix_all_audio', False)
                active = [
                    GridSource(path=s['path'], offset=s['offset'],
                               has_audio=s.get('has_audio', True))
                    for s in sources
                ]
                try:
                    if len(active) == 1:
                        src = active[0]
                        self.ffmpeg.run(
                            ['ffmpeg', '-y', '-v', 'error',
                             '-ss', str(src.offset), '-i', src.path,
                             '-t', str(duration),
                             '-vf', f'scale={target_w}:{target_h}:'
                                    f'force_original_aspect_ratio=decrease,'
                                    f'pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1',
                             *self.ffmpeg.get_video_encoder_fast(),
                             '-pix_fmt', 'yuv420p',
                             '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
                             '-r', '25', seg_path],
                            description=f'grid {i} (1 webcam, {duration:.0f}s)',
                        )
                    else:
                        self.grid.composite(active, duration, seg_path,
                                            target_w, target_h,
                                            mix_all_audio=mix_audio)
                    with_audio = os.path.join(tmp_dir, f'grida_{i}.mp4')
                    final_seg = self.segments.ensure_audio(seg_path, with_audio)
                    concat_segments.append(final_seg)
                except subprocess.CalledProcessError:
                    logging.warning(f'Grid segment {i} failed, using black gap')
                    gap_path = os.path.join(tmp_dir, f'gridfail_{i}.mp4')
                    self.segments.generate_black(gap_path, duration,
                                                 target_w, target_h, target_pix_fmt)
                    concat_segments.append(gap_path)

            elif seg['type'] == 'presenter':
                os.makedirs(grid_dir, exist_ok=True)
                seg_path = os.path.join(grid_dir, f'presenter_{i}.mp4')
                duration = seg['planned_duration']
                ms = seg['main_source']
                main = GridSource(path=ms['path'], offset=ms['offset'],
                                  has_audio=ms.get('has_audio', True),
                                  is_admin=ms.get('is_admin', False))
                overlay = None
                if seg.get('overlay_source'):
                    os_ = seg['overlay_source']
                    overlay = GridSource(path=os_['path'], offset=os_['offset'],
                                        has_audio=os_.get('has_audio', True),
                                        is_admin=os_.get('is_admin', False))
                extra = [
                    GridSource(path=s['path'], offset=s['offset'],
                               has_audio=s.get('has_audio', True))
                    for s in seg.get('extra_audio_sources', [])
                ]
                try:
                    self.grid.presenter_composite(
                        main, overlay, extra, duration, seg_path,
                        target_w, target_h,
                        mix_all_audio=seg.get('mix_all_audio', True),
                    )
                    with_audio = os.path.join(tmp_dir, f'presenta_{i}.mp4')
                    final_seg = self.segments.ensure_audio(seg_path, with_audio)
                    concat_segments.append(final_seg)
                except subprocess.CalledProcessError:
                    logging.warning(f'Presenter segment {i} failed, using black gap')
                    gap_path = os.path.join(tmp_dir, f'presfail_{i}.mp4')
                    self.segments.generate_black(gap_path, duration,
                                                 target_w, target_h, target_pix_fmt)
                    concat_segments.append(gap_path)

            elif seg['type'] == 'video':
                norm_path = os.path.join(tmp_dir, f'norm_{i}.mp4')
                source_offset = seg.get('source_offset', 0)
                planned_dur = seg.get('planned_duration', 0)
                try:
                    self.segments.normalize(
                        seg['source_path'], norm_path,
                        target_w, target_h, target_pix_fmt,
                        max_duration=planned_dur,
                        seek=source_offset,
                    )
                    with_audio_path = os.path.join(tmp_dir, f'norma_{i}.mp4')
                    final_seg = self.segments.ensure_audio(
                        norm_path, with_audio_path)
                except subprocess.CalledProcessError:
                    logging.warning(
                        f'Video segment {i} failed, using black gap')
                    gap_dur = planned_dur or seg.get('source_duration', 0)
                    gap_path = os.path.join(tmp_dir, f'vidfail_{i}.mp4')
                    self.segments.generate_black(gap_path, gap_dur,
                                                 target_w, target_h,
                                                 target_pix_fmt)
                    concat_segments.append(gap_path)
                    continue

                actual_dur = self.prober.get_duration(final_seg)
                if planned_dur > 0 and actual_dur < planned_dur - 1.0:
                    shortfall = planned_dur - actual_dur
                    logging.warning(
                        f'Segment {i} is {actual_dur:.1f}s but planned '
                        f'{planned_dur:.1f}s — padding {shortfall:.1f}s'
                    )
                    pad_path = os.path.join(tmp_dir, f'pad_{i}.mp4')
                    self.segments.generate_black(pad_path, shortfall,
                                                 target_w, target_h, target_pix_fmt)
                    concat_segments.append(final_seg)
                    concat_segments.append(pad_path)
                else:
                    concat_segments.append(final_seg)

        # Concatenate
        concat_list_path = os.path.join(tmp_dir, 'concat.txt')
        with open(concat_list_path, 'w') as f:
            for seg in concat_segments:
                f.write(f"file '{os.path.abspath(seg)}'\n")

        video_only_path = os.path.join(tmp_dir, 'video_concat.mp4')
        logging.info(f'Concatenating {len(concat_segments)} segments...')

        cap = manifest.get('max_duration') or total_duration
        concat_cmd = [
            'ffmpeg', '-y', '-v', 'warning',
            '-f', 'concat', '-safe', '0',
            '-i', concat_list_path, '-c', 'copy',
        ]
        if cap:
            concat_cmd.extend(['-t', str(cap)])
        concat_cmd.append(video_only_path)
        self.ffmpeg.run(concat_cmd, description=f'concat {len(concat_segments)} segments')

        # Composite slides
        slide_plan = manifest.get('slide_compositing')
        if slide_plan and slide_plan.get('events'):
            composited_path = os.path.join(tmp_dir, 'video_composited.mp4')
            self.slides.composite(
                video_only_path, slide_plan['events'], composited_path,
                tmp_dir, total_duration,
            )
            video_only_path = composited_path

        # Merge audio
        if audio_files:
            logging.info(f'Merging {len(audio_files)} audio tracks...')
            result_path = self.audio.merge(
                video_only_path, audio_files, tmp_dir, output_path,
                total_duration,
            )
        else:
            shutil.move(video_only_path, output_path)
            result_path = output_path

        shutil.rmtree(tmp_dir, ignore_errors=True)
        logging.info(f'Final video saved to {result_path}')
        return result_path

    def process(self, total_duration, downloaded_files, directory,
                output_path, max_duration=None, slide_events=None,
                timeline=None):
        """Full pipeline: analyze then execute."""
        manifest = self.analyze(
            total_duration, downloaded_files, directory, output_path,
            max_duration, slide_events, timeline=timeline,
        )
        return self.execute(manifest)


# --- Backward-compatible module-level functions ---

def process_and_download_clips(directory, json_data):
    """Parse API JSON for chunks, slides, admin IDs.

    Returns:
        (total_duration, tagged_chunks, deduped_slides, timeline)
        where timeline is a StreamTimeline instance built from mediasession events.
        tagged_chunks are DownloadChunk-compatible tuples for backward compatibility
        with the download pipeline.
    """
    total_duration = float(json_data.get('duration', 0))
    if not total_duration:
        raise ValueError('Duration not found in JSON data.')

    timeline = StreamTimeline()
    timeline.build(json_data)

    # Convert to tuples for backward compatibility with download_chunks_parallel
    tagged_chunks = [
        (c.url, c.start_time, c.conf_id, c.is_admin, c.api_duration)
        for c in timeline.get_download_chunks()
    ]

    deduped_slides = timeline.get_slide_events_as_dicts()

    return total_duration, tagged_chunks, deduped_slides, timeline


def compile_final_video(total_duration, downloaded_files, directory,
                        output_path, max_duration, slide_events=None,
                        timeline=None):
    """Backward-compatible entry point."""
    processor = VideoProcessor()
    processor.process(total_duration, downloaded_files, directory,
                      output_path, max_duration, slide_events,
                      timeline=timeline)

