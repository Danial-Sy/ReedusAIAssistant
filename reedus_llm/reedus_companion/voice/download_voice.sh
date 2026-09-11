#!/usr/bin/env bash
set -euo pipefail

echo ">> Warming up Kokoro and writing a preview (first run downloads the model)…"
python -m reedus_companion.voice.tts "Hey! It's Reedus. Yeah, I sound a little robotic. I think it's kinda cute though."

echo ""
echo ">> Preview written to reedus_companion/models/voice/reedus_preview.wav"
echo ">> Tune the voice live, e.g.:"
echo "     REEDUS_VOICE=af_sky REEDUS_PITCH=4 REEDUS_ROBOT=0.15 \\"
echo "       python -m reedus_companion.voice.tts \"testing one two three\""
echo "   voices: af_heart af_sky af_bella af_nicole af_nova   |   pitch: semitones up   |   robot: 0..1"
