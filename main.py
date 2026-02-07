from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import io
import wave
import numpy as np

app = FastAPI(title="Master Backend (WAV only)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health():
    return {"status": "ok"}


def _read_wav(file_bytes: bytes):
    """
    Lee WAV PCM (16-bit o 24-bit) mono/stereo y devuelve (audio_float32, sr, channels)
    audio_float32 rango aproximado [-1, 1], shape (n_samples, channels)
    """
    bio = io.BytesIO(file_bytes)

    try:
        with wave.open(bio, "rb") as wf:
            channels = wf.getnchannels()
            sr = wf.getframerate()
            sampwidth = wf.getsampwidth()  # bytes per sample (2=16bit, 3=24bit, 4=32bit)
            nframes = wf.getnframes()
            frames = wf.readframes(nframes)
    except wave.Error:
        raise HTTPException(status_code=400, detail="Archivo inválido o no es WAV PCM soportado.")

    if channels not in (1, 2):
        raise HTTPException(status_code=400, detail="Solo se soporta WAV mono o stereo (1-2 canales).")

    if sampwidth == 2:
        # 16-bit signed little endian
        audio_i16 = np.frombuffer(frames, dtype=np.int16)
        audio = audio_i16.astype(np.float32) / 32768.0
    elif sampwidth == 3:
        # 24-bit signed little endian: convertir manualmente a int32 con signo
        b = np.frombuffer(frames, dtype=np.uint8)
        if len(b) % 3 != 0:
            raise HTTPException(status_code=400, detail="WAV 24-bit corrupto.")
        b = b.reshape(-1, 3)

        # little endian: byte0 + byte1<<8 + byte2<<16
        x = (b[:, 0].astype(np.int32) |
             (b[:, 1].astype(np.int32) << 8) |
             (b[:, 2].astype(np.int32) << 16))

        # sign extend 24->32
        x = (x << 8) >> 8
        audio = x.astype(np.float32) / 8388608.0
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Sample width no soportado: {sampwidth*8} bits. Usa WAV PCM 16-bit o 24-bit."
        )

    # reshape a (n_samples, channels)
    audio = audio.reshape(-1, channels)
    return audio, sr, channels


def _write_wav_16bit(audio_float32: np.ndarray, sr: int, channels: int) -> io.BytesIO:
    """
    Escribe WAV PCM 16-bit little endian en memoria.
    audio_float32 shape (n_samples, channels)
    """
    # clamp
    audio = np.clip(audio_float32, -1.0, 1.0)

    # float -> int16
    audio_i16 = (audio * 32767.0).astype(np.int16)
    interleaved = aud
