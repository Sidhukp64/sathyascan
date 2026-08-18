"""
Bounded video frame extraction (Phase 5b). Samples at most `max_frames`
frames from the video, spaced `sampling_interval_seconds` apart, and hands
each one back as real JPEG bytes — so the EXISTING OCRTool interface
(`OCRTool.extract(image_bytes, mime_type, language_hint)`, unchanged since
Phase 3) can be reused directly, with zero new OCR logic.

Resource-exhaustion guards, all hard caps (the user's explicit Phase 5
requirement — "never process unlimited frames", "keep memory usage
bounded"):
  - max_frames: absolute ceiling on frames returned, regardless of video
    length — decoding STOPS once this many frames have been sampled, it
    does not decode the whole video first and then truncate.
  - sampling_interval_seconds: frames closer together than this are skipped
    during decode (cheap — PyAV lets us skip without fully decoding
    intermediate frames in most codecs' fast paths).
  - Frames are converted to JPEG and discarded from memory as soon as their
    bytes are captured — no list of raw VideoFrame/PIL Image objects is kept
    around longer than one iteration.

No frame images are ever persisted to any table or disk — only the
extracted TEXT (via OCRTool, called by the caller) and a timestamp are kept,
consistent with Phase 3's "no blob storage" precedent (see
app/models/video_frame.py).
"""

import io
from dataclasses import dataclass

import av
from PIL import Image


class FrameExtractionError(Exception):
    pass


@dataclass(frozen=True)
class ExtractedFrame:
    timestamp_seconds: float
    jpeg_bytes: bytes


def extract_frames(
    video_bytes: bytes,
    *,
    max_frames: int,
    sampling_interval_seconds: float,
) -> list[ExtractedFrame]:
    """Decodes at most `max_frames` frames, spaced at least
    `sampling_interval_seconds` apart, converting each to JPEG in memory.
    Never raises for "video too short to reach max_frames" — returns
    whatever was actually found (possibly zero frames for a video with no
    decodable video stream, though validate_video should have already
    rejected that case)."""
    try:
        container = av.open(io.BytesIO(video_bytes))
    except Exception as exc:  # noqa: BLE001 - PyAV's own exception hierarchy per-format
        raise FrameExtractionError("could not open video container") from exc

    frames: list[ExtractedFrame] = []
    try:
        video_streams = container.streams.video
        if not video_streams:
            return frames

        next_sample_at = 0.0
        for frame in container.decode(video_streams[0]):
            if len(frames) >= max_frames:
                break  # hard stop — never decode/process more than the ceiling

            timestamp = float(frame.time) if frame.time is not None else 0.0
            if timestamp < next_sample_at:
                continue  # skip frames closer together than the sampling interval

            image = frame.to_image()  # PIL Image, RGB
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG")
            frames.append(ExtractedFrame(timestamp_seconds=timestamp, jpeg_bytes=buffer.getvalue()))

            next_sample_at = timestamp + sampling_interval_seconds
    except Exception as exc:  # noqa: BLE001 - a mid-decode failure must not crash the pipeline
        raise FrameExtractionError("frame decoding failed") from exc
    finally:
        container.close()

    return frames
