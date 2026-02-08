import os
import uuid
import shutil
import subprocess
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

app = FastAPI()

# Servir la web (index, dashboard, master)
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")

def build_chain(preset: str, intensity: int):
    base = "highpass=f=30"

    preset = (preset or "clean").lower()

    if preset == "club":
        eq = "equalizer=f=80:width_type=o:width=1:g=3"
    elif preset == "warm":
        eq = "equalizer=f=200:width_type=o:width=1:g=2"
    elif preset == "bright":
        eq = "equalizer=f=12000:width_type=o:width=1:g=3"
    elif preset == "heavy":
        eq = "equalizer=f=120:width_type=o:width=1:g=3"
    else:
        eq = "equalizer=f=100:width_type=o:width=1:g=1.5"

    target_lufs = -14 + (intensity / 100) * 6
    return f"{base},{eq},alimiter=limit=0.97,loudnorm=I={target_lufs}:TP=-1.0:LRA=11"

@app.post("/api/master")
async def master_audio(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
):
    job_id = uuid.uuid4().hex
    in_path = TMP_DIR / f"in_{job_id}.wav"
    out_path = TMP_DIR / f"master_{job_id}.wav"

    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    chain = build_chain(preset, intensity)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-af", chain,
        "-c:a", "pcm_s16le",
        str(out_path)
    ]

    subprocess.run(cmd, check=True)

    try:
        os.remove(in_path)
    except Exception:
        pass

    return FileResponse(
        out_path,
        media_type="audio/wav",
        filename="warmaster_master.wav"
    )

