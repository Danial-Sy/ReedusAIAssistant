# Reedus relay. Build context is the REPO ROOT (the relay imports reedus_llm/reedus_companion):
#   docker build -f ReedusLiteS3/relay/Dockerfile .
# Railway does this via ReedusLiteS3/relay/railway.json (root directory = repo root).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/reedus_llm:/app/relay \
    RELAY_DATA=/data \
    REEDUS_WAKEWORD_MODEL=/app/relay/models/hey_reedus.onnx

WORKDIR /app
COPY ReedusLiteS3/relay/requirements.txt /app/relay/requirements.txt
RUN pip install --no-cache-dir -r /app/relay/requirements.txt \
 && python -c "import openwakeword.utils as u; u.download_models(model_names=[])"

# only the brain modules the relay uses: no models, no memory/, no training data
COPY reedus_llm/reedus_companion/__init__.py reedus_llm/reedus_companion/config.py \
     reedus_llm/reedus_companion/mode.py /app/reedus_llm/reedus_companion/
COPY reedus_llm/reedus_companion/cloud /app/reedus_llm/reedus_companion/cloud
COPY reedus_llm/reedus_companion/assist /app/reedus_llm/reedus_companion/assist
COPY reedus_llm/reedus_companion/lite /app/reedus_llm/reedus_companion/lite
COPY reedus_llm/reedus_companion/serve/__init__.py reedus_llm/reedus_companion/serve/persona.py \
     /app/reedus_llm/reedus_companion/serve/
COPY reedus_llm/reedus_companion/voice/__init__.py reedus_llm/reedus_companion/voice/tts_inworld.py \
     reedus_llm/reedus_companion/voice/voice_prefs.py reedus_llm/reedus_companion/voice/speech_text.py \
     reedus_llm/reedus_companion/voice/wakeword.py /app/reedus_llm/reedus_companion/voice/
# his voice examples (cloud/persona_shots.py), the same few-shots the run box's cloud mode uses
COPY reedus_llm/reedus_companion/data/__init__.py reedus_llm/reedus_companion/data/reedus_voice.jsonl \
     /app/reedus_llm/reedus_companion/data/
COPY ReedusLiteS3/relay /app/relay
# the ReedOS app, served at the relay's root so it works with the Xeon off
COPY ReedOS/reedos-app/src /app/ui

RUN mkdir -p /data /app/reedus_llm/reedus_companion/memory
EXPOSE 8080
CMD ["python", "-m", "reedus_relay"]
