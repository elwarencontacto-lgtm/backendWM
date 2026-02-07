from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import io
import wave
import numpy as np

app = FastAPI(title="Master Backend")

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
    return len(b) >= 12 and b[0:4] == b"RIFF" and b[8:12] == b"WAVE"

def read_wav_pcm(file_bytes: bytes):
    bio = io.BytesIO(file_bytes)
    try:
        with wave.open(bio, "rb") as wf:
            channels = wf.getnchannels()
            sr = wf.getframerate()
            sampwidth = wf.getsampwidth()  # 2=16bit, 3=24bit
            nframes = wf.getnframes()
            frames = wf.readframes(nframes)
    except wave.Error:
        raise HTTPException(status_code=400, detail="El archivo no es un WAV válido o está dañado.")

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
        x = (x << 8) >> 8
        audio = x.astype(np.float32) / 8388608.0
    else:
        raise HTTPException(status_code=400, detail="Solo se soporta WAV PCM 16-bit o 24-bit.")

    audio = audio.reshape(-1, channels)
    return audio, sr, channels

def write_wav_16bit(audio_float32: np.ndarray, sr: int, channels: int) -> io.BytesIO:
    audio = np.clip(audio_float32, -1.0, 1.0)
    audio_i16 = (audio * 32767.0).astype(np.int16)
    interleaved = audio_i16.reshape(-1).tobytes()

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(interleaved)

    out.seek(0)
    return out

@app.post("/master")
async def master(file: UploadFile = File(...)):
    try:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Archivo vacío.")

        if not is_wav_bytes(data):
            raise HTTPException(status_code=400, detail="Por ahora solo se acepta WAV (tu web debería enviar WAV).")

        audio, sr, channels = read_wav_pcm(data)

        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0:
            audio = (audio / peak) * 0.95

        out = write_wav_16bit(audio, sr, channels)

        return StreamingResponse(
            out,
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=master.wav"}
        )

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Error interno: {str(e)}"})

