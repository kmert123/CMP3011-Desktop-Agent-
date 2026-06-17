"""PyAudio recording and silence-detection logic."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import pyaudio

import config


def _rms(chunk_bytes: bytes) -> float:
    """Return the RMS amplitude of a 16-bit PCM chunk."""
    samples = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32)
    return math.sqrt(float(np.mean(samples ** 2)))


def record_until_silence() -> bytes:
    """Record from the default microphone until silence or max duration."""
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=config.CHANNELS,
        rate=config.SAMPLE_RATE,
        input=True,
        frames_per_buffer=config.CHUNK_SIZE,
    )

    frames: list[bytes] = []
    silent_chunks = 0
    chunks_per_second = config.SAMPLE_RATE / config.CHUNK_SIZE
    silence_limit = config.SILENCE_DURATION_SEC * chunks_per_second
    max_chunks = config.MAX_RECORDING_SEC * chunks_per_second

    try:
        while len(frames) < max_chunks:
            chunk = stream.read(config.CHUNK_SIZE, exception_on_overflow=False)
            frames.append(chunk)
            if _rms(chunk) < config.SILENCE_THRESHOLD_RMS:
                silent_chunks += 1
            else:
                silent_chunks = 0
            if silent_chunks >= silence_limit:
                break
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    return b"".join(frames)


if __name__ == "__main__":
    import wave

    print("Speak — recording until silence...")
    pcm = record_until_silence()
    duration = len(pcm) / (config.SAMPLE_RATE * config.CHANNELS * 2)

    out_path = Path(tempfile.gettempdir()) / "jarvis_voice_test.wav"
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(config.CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(config.SAMPLE_RATE)
        wf.writeframes(pcm)

    print(out_path)
    print(f"{duration:.2f}s")
