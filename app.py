import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_ffmpeg(cmd: list[str]) -> None:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="ffmpeg no está instalado en el servidor.")

    if proc.returncode != 0:
        err = (proc.stderr or "")[-2000:]
        raise HTTPException(status_code=500, detail=f"FFmpeg error:\n{err}")


def preset_chain(preset: str, intensity: int) -> str:
    intensity = max(0, min(100, int(intensity)))
    thr = -18.0 - (intensity * 0.10)
    ratio = 2.0 + (intensity * 0.04)
    makeup = 2.0 + (intensity * 0.06)
    limit = -1.0

    preset = (preset or "clean").strip().lower()

    if preset == "club":
        eq = "equalizer=f=80:t=q:w=1.0:g=3, equalizer=f=9000:t=q:w=1.0:g=2"
    elif preset == "warm":
        eq = "equalizer=f=160:t=q:w=1.0:g=2, equalizer=f=4500:t=q:w=1.0:g=-1"
    elif preset == "bright":
        eq = "equalizer=f=120:t=q:w=1.0:g=-1, equalizer=f=8500:t=q:w=1.0:g=3"
    elif preset == "heavy":
        eq = "equalizer=f=90:t=q:w=1.0:g=3, equalizer=f=3000:t=q:w=1.0:g=2"
    else:
        eq = "equalizer=f=120:t=q:w=1.0:g=1, equalizer=f=8000:t=q:w=1.0:g=1"

    return (
        f"{eq}, "
        f"acompressor=threshold={thr}dB:ratio={ratio}:attack=12:release=120:makeup={makeup}, "
        f"alimiter=limit={limit}dB"
    )


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/master")
async def master_api(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Falta archivo.")

    job_id = uuid.uuid4().hex[:10]
    in_path = TMP_DIR / f"in_{job_id}_{file.filename}"
    out_path = TMP_DIR / f"warmaster_master_{job_id}.wav"

    try:
        with in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No pude guardar el archivo: {e}")

    af = preset_chain(preset, intensity)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-vn",
        "-af", af,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s32",
        str(out_path),
    ]

    run_ffmpeg(cmd)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise HTTPException(status_code=500, detail="El master salió vacío o no se generó.")

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers={"Cache-Control": "no-store"},
    )


# ✅ Monta estáticos AL FINAL para no tapar /api/*
if PUBLIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
