import os
import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

app = FastAPI()

# CORS (si tu frontend está en otro dominio). Si un día lo restringes, cambia ["*"].
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg error:\n{proc.stderr[-8000:]}",
        )

def preset_chain(preset: str, intensity: int) -> str:
    intensity = max(0, min(100, int(intensity)))

    thr = -18.0 - (intensity * 0.10)
    ratio = 2.0 + (intensity * 0.04)
    makeup = 2.0 + (intensity * 0.06)

    preset = (preset or "clean").lower()

    if preset == "club":
        eq = "bass=g=4:f=90,treble=g=2:f=9000"
    elif preset == "warm":
        eq = "bass=g=3:f=160,treble=g=-2:f=4500"
    elif preset == "bright":
        eq = "bass=g=-1:f=120,treble=g=4:f=8500"
    elif preset == "heavy":
        eq = "bass=g=5:f=90,treble=g=2:f=3500"
    else:
        eq = "bass=g=2:f=120,treble=g=1:f=8000"

    return (
        f"{eq},"
        f"acompressor=threshold={thr}dB:ratio={ratio}:attack=12:release=120:makeup={makeup},"
        f"alimiter=limit=-1.0dB"
    )

def safe_filename(name: str) -> str:
    # deja solo caracteres seguros
    clean = "".join(c for c in (name or "") if c.isalnum() or c in "._-").strip("._-")
    return clean or "audio.wav"

def cleanup_files(*paths: Path) -> None:
    for p in paths:
        try:
            if p and p.exists():
                p.unlink()
        except Exception:
            pass

@app.get("/api/health")
def health():
    return {"ok": True}

@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido")

    job_id = uuid.uuid4().hex[:8]
    safe_name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{job_id}_{safe_name}"
    out_path = TMP_DIR / f"master_{job_id}.wav"

    # guardar archivo
    try:
        with in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    filters = preset_chain(preset, intensity)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s32",
        str(out_path)
    ]

    run_ffmpeg(cmd)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, out_path)
        raise HTTPException(status_code=500, detail="Master no generado o vacío")

    # ✅ borrar input+output después de entregar la respuesta
    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        background=BackgroundTask(cleanup_files, in_path, out_path),
    )

# Para ejecutar local:
# uvicorn app:app --reload --host 0.0.0.0 --port 3000
