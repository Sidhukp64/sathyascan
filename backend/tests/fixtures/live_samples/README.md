# Live validation sample media

This directory is **empty by default** (not committed with real audio/video —
none exists in this repository as of this writing). It is the drop-in
location the live-credentialed tests (`tests/integration/test_sarvam_stt_live.py`,
`tests/integration/test_resemble_forensics_live.py`) look for **real** speech
and media samples, so that live validation can test actual transcription
accuracy and forensics behavior, not just "did the API respond."

## What to add, and exactly what it unlocks

| File | Format | Unlocks |
|---|---|---|
| `malayalam.ogg` | OGG/Opus, WAV, or MP3 — a real Malayalam voice recording with a spoken factual claim | Real Malayalam STT accuracy validation |
| `tamil.ogg` | same | Real Tamil STT accuracy validation |
| `hindi.ogg` | same | Real Hindi STT accuracy validation |
| `english.ogg` | same | Real English STT accuracy validation |
| `authentic_audio.wav` | any supported audio format — genuinely human-recorded, not AI-generated | Real audio-forensics "authentic" case |
| `synthetic_audio.wav` | a known AI-generated/voice-cloned clip | Real audio-forensics "AI-generated" case |
| `authentic_video.mp4` | a genuine, unedited video clip | Real video-forensics "authentic" case |
| `synthetic_video.mp4` | a known AI-generated/deepfake video clip | Real video-forensics "AI-generated" case |

## What happens if a file is missing

Each live test independently checks for its own file. If a specific file is
absent, that specific test **skips with an explicit, honest reason** — it
does not fail, and it does not fall back to a synthetic/silent clip and
claim that proves transcription accuracy or forensics correctness. A
synthetic fallback can only prove "the real API round-tripped successfully"
(auth worked, the request/response shapes matched), never "the transcript is
linguistically correct" or "the forensics verdict is right" — those claims
require real content, and the tests are written to never blur that line.

## Also required regardless of sample files

None of these tests will run their real-API branch at all without real
provider credentials: `SPEECH_TO_TEXT_API_KEY` (Sarvam AI) for the STT
tests, `AUDIO_FORENSICS_API_KEY`/`VIDEO_FORENSICS_API_KEY` (Resemble AI
Detect) for the forensics tests. Without a key, the corresponding test
skips with a clear "credential not set" reason — see each test file's
module docstring.

## Privacy note

Whatever you place here is real, potentially identifying audio/video. This
directory should never be committed to version control with real personal
recordings in it — add it to `.gitignore` if you populate it, and prefer
consented, non-identifying, or synthetic-benchmark-style samples over
anything that could identify a real person without their consent.
