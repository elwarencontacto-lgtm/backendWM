from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import numpy as np
import soundfile as sf
import io

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

@app.post("/master")
async def master_audio(file: UploadFile = File(...)):
    try:
        audio_bytes = await file.read()
        audio_buffer = io.BytesIO(audio_bytes)

        audio, sr = sf.read(audio_buffer, always_2d=True)

        # ===== MASTERING BÁSICO SEGURO =====
        peak = np.max(np.abs(audio))
        if peak > 0:
            audio = audio / peak * 0.95
        # ==================================

        output_buffer = io.BytesIO()
        sf.write(
            output_buffer,
            audio,
            sr,
            format="WAV",
            subtype="PCM_16"
        )
        output_buffer.seek(0)

        return StreamingResponse(
            output_buffer,
            media_type="audio/wav",
            headers={
                "Content-Disposition": "attachment; filename=master.wav"
            }
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
