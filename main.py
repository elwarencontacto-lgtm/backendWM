from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import io
import wave
import numpy as np

app = FastAPI(title="Master Backend (WAV export)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok"}

def is_wav_bytes(b: bytes) -> bool:
    # WAV típico empieza con "RIFF" y contiene "WAVE" al inicio
    return len(b) >= 12 and b[0:4] == b"RIFF" and b[8:12] == b"WAVE"

def read_wav_pcm(file_bytes: bytes):
    """
    Lee WAV PCM (16-bit o 24-bit) mono/stereo y devuelve:
    audio_float32 (n_samples, channels), sample_rate, channels
    """
    bio = io.BytesIO(file_bytes)

    try:
        with wave.open(bio, "rb") as wf:
            channels = wf.getnchannels()
            sr = wf.getframerate()
            sampwidth = wf.getsampwidth()  # bytes (2=16bit, 3=24bit)
            nframes = wf.getnframes()
            frames = wf.readframes(nframes)
    except wave.Error:
        raise HTTPException(status_code=400, detail="El archivo no es un WAV válido (o está dañado).")

    if channels not in (1, 2):
        raise HTTPException(status_code=400, detail="Solo se soporta WAV mono o stereo (1 o 2 canales).")

    if sampwidth == 2:
        audio_i16 = np.frombuffer(frames, dtype=np.int16)
        audio = audio_i16.astype(np.float32) / 32768.0
    elif sampwidth == 3:
        b = np.frombuffer(frames, dtype=np.uint8)
        if len(b) % 3 != 0:
            raise HTTPException(status_code=400, detail="WAV 24-bit corrupto.")
        b = b.reshape(-1, 3)
        x = (b[:, 0].astype(np.int32) |
             (b[:, 1].astype(np.int32) << 8) |
             (b[:, 2].astype(np.int32) << 16))
        x = (x << 8) >> 8  # signo
