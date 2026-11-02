"""idle listening driven by the wake-word model."""
from __future__ import annotations

import time
from typing import Optional

import numpy as np

from reedus_companion import config
from reedus_companion.voice import events
from reedus_companion.voice.wakeword import FRAME_SAMPLES, WakeWordDetector, shared_detector


class WakeListener:
    """Owns the mic while idle: waits for the name, then records the command."""

    def __init__(self, detector: Optional[WakeWordDetector] = None):
        self.det = detector if detector is not None else shared_detector()

    @property
    def available(self) -> bool:
        return bool(self.det and self.det.available)

    def wait_and_capture(self, stop_check=None, poll: float = 0.05):
        """Block until the wake word, then return the utterance after it."""
        import sounddevice as sd

        sr = int(config.MIC_SAMPLE_RATE)
        silence_sec = float(getattr(config, "MIC_SILENCE_SEC", 1.4))
        max_sec = float(getattr(config, "MIC_MAX_SEC", 45))
        rms_floor = float(getattr(config, "MIC_RMS", 0.012))
        frame_s = FRAME_SAMPLES / sr

        self.det.reset()
        with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                            blocksize=FRAME_SAMPLES, device=config.MIC_DEVICE) as stream:
            # phase 1: wait for the name
            while True:
                if stop_check is not None and stop_check():
                    return None
                block, _of = stream.read(FRAME_SAMPLES)
                if self.det.feed(block[:, 0]):
                    events.emit("wake")
                    events.emit("state", state="listening")
                    break
                time.sleep(0)          # yield, the read already paces us

            # phase 2: record what follows
            hangover = float(getattr(config, "MIC_HANGOVER", 0.28))
            silent_needed = max(1, int(silence_sec / frame_s))
            max_frames = int(max_sec / frame_s)
            lead_frames = max(1, int(1.2 / frame_s))

            frames, silent_run, started, speech_peak = [], 0, False, rms_floor
            for i in range(max_frames):
                block, _of = stream.read(FRAME_SAMPLES)
                mono = block[:, 0]
                frames.append(mono.copy())
                level = float(np.sqrt(np.mean(mono ** 2)))
                if level > rms_floor:
                    started, silent_run = True, 0
                    speech_peak = max(speech_peak * 0.97, level)
                elif started:
                    end_thresh = max(rms_floor * 0.55, speech_peak * hangover)
                    if level < end_thresh:
                        silent_run += 1
                        if silent_run >= silent_needed:
                            break
                    else:
                        silent_run = 0
                    speech_peak = max(speech_peak * 0.97, rms_floor)
                elif i >= lead_frames:
                    return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
