"""Whisper-based audio-to-text transcription."""

from __future__ import annotations

import numpy as np

import config

_model = None


def _get_model():
    global _model
    if _model is None:
        import whisper
        _model = whisper.load_model(config.WHISPER_MODEL)
    return _model


def transcribe_pcm(
    pcm_bytes: bytes,
    sample_rate: int = config.SAMPLE_RATE,
    initial_prompt: str = "",
) -> str:
    """Convert 16-bit PCM bytes to text via Whisper.

    initial_prompt, if non-empty, is forwarded to Whisper to bias decoding toward
    the named tokens (app names, proper nouns).  Whisper treats it as prior context
    so recognition favours its vocabulary without forcing exact matches.
    """
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if sample_rate != 16000:
        import scipy.signal as sps
        num_samples = int(len(audio) * 16000 / sample_rate)
        audio = sps.resample(audio, num_samples)
    kwargs: dict = {"language": config.WHISPER_LANGUAGE, "fp16": False}
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    result = _get_model().transcribe(audio, **kwargs)
    return result["text"].strip()


if __name__ == "__main__":
    import pyaudio

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=config.CHANNELS,
        rate=config.SAMPLE_RATE,
        input=True,
        frames_per_buffer=config.CHUNK_SIZE,
    )
    print("Recording 5 seconds...")
    frames = [stream.read(config.CHUNK_SIZE) for _ in range(int(config.SAMPLE_RATE / config.CHUNK_SIZE * 5))]
    stream.stop_stream()
    stream.close()
    pa.terminate()

    print(transcribe_pcm(b"".join(frames)))
