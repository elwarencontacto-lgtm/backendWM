from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import subprocess
import uuid
import os
import json

app = FastAPI()

# ✅ CORS — SOLUCIÓN AL "FAILED TO FETCH"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Carpeta temporal
TMP_DIR = "tmp"
os.makedirs(TMP_DIR, exist_ok=True)

# ---------- TEST ----------
@app.get("/")
def root():
    return {"ok": True}

# ---------- EXPORT WAV ----------
@app.post("/export-wav")
async def export_wav(
    audio: UploadFile = File(...),
    settings: str = Form(...)
):
    try:
        # Parse settings
        cfg = json.loads(settings)

        # Archivos temporales
        uid = str(uuid.uuid4())
        input_path = f"{TMP_DIR}/{uid}_in"
        output_path = f"{TMP_DIR}/{uid}_out.wav"

        # Guardar audio recibido
        with open(input_path, "wb") as f:
            f.write(await audio.read())

        # 🔊 PROCESAMIENTO BÁSICO (puedes mejorar después)
        # Normaliza + limitador simple
        cmd = [
            "ffmpeg",
            "-y",
            "-i", input_path,
            "-af", "loudnorm=I=-14:LRA=11:TP=-1.5",
            output_path
        ]

        subprocess.run(cmd, check=True)

        # Enviar WAV final
        return FileResponse(
            output_path,
            media_type="audio/wav",
            filename="master.wav"
        )

    except subprocess.CalledProcessError as e:
        return JSONResponse(
            status_code=500,
            content={"error": "FFmpeg failed", "details": str(e)}
        )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": "Server error", "details": str(e)}
        )

    finally:
        # Limpieza
        try:
            if os.path.exists(input_path):
                os.remove(input_path)
        except:
            pass
            
