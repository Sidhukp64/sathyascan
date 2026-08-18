"""
Audio-track extraction from a video container (Phase 5b). Decodes the
video's audio stream and re-encodes it to PCM WAV in memory — a real
transcode (not a raw stream remux) so the result is always a standard,
uncompressed format any SpeechToTextProvider can accept, matching
app/media/audio_validation.py's SUPPORTED_AUDIO_MIME_TYPES (`audio/wav`)
with no new format-support work needed there.

Bounded like every other Phase 5 media operation: if the video has no audio
stream at all, returns None rather than raising — VideoPipeline treats a
silent/audio-less video as "no spoken claim available", not an error.
"""

import io

import av


class AudioExtractionError(Exception):
    pass


_OUTPUT_SAMPLE_RATE = 16_000  # standard for speech models; keeps output size small


def extract_audio_track(video_bytes: bytes) -> bytes | None:
    """Returns WAV bytes for the video's audio track, or None if the video
    has no audio stream. Raises AudioExtractionError on a genuine decode
    failure (corrupt audio stream) — distinct from "no audio stream present
    at all", which is not an error."""
    try:
        in_container = av.open(io.BytesIO(video_bytes))
    except Exception as exc:  # noqa: BLE001 - PyAV's own exception hierarchy per-format
        raise AudioExtractionError("could not open video container") from exc

    try:
        audio_streams = in_container.streams.audio
        if not audio_streams:
            return None

        out_buffer = io.BytesIO()
        try:
            out_container = av.open(out_buffer, mode="w", format="wav")
            out_stream = out_container.add_stream("pcm_s16le", rate=_OUTPUT_SAMPLE_RATE)
            resampler = av.AudioResampler(format="s16", layout="mono", rate=_OUTPUT_SAMPLE_RATE)

            for frame in in_container.decode(audio_streams[0]):
                for resampled_frame in resampler.resample(frame):
                    for packet in out_stream.encode(resampled_frame):
                        out_container.mux(packet)
            for packet in out_stream.encode(None):
                out_container.mux(packet)
            out_container.close()
        except Exception as exc:  # noqa: BLE001
            raise AudioExtractionError("audio track decode/encode failed") from exc

        return out_buffer.getvalue()
    finally:
        in_container.close()
