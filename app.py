import os
import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

app = FastAPI()


def build_chain(preset: str, intensity: int) -> str:
    preset = (preset or "clean").lower()

    try:
        intensity = int(intensity)
    except:
        intensity = 55

    intensity = max(0, min(100, intensity))

    # EQ simple (seguro)
    if preset == "club":
        eq = "bass=g=5"
    elif preset == "warm":
        eq = "bass=g=3,treble=g=-1"
    elif preset == "bright":
        eq = "treble=g=4"
    elif preset == "heavy":
        eq = "bass=g=6,treble=g=2"
    else:
        eq = "bass=g=2,treble=g=1"

    # Ganancia según intensidad
    gain = 3 + (intensity / 100) * 5  # 3 a 8 dB

    return f"{eq},volume={gain}dB,alimiter=limit=0.98"

    loudnorm = f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11"
    return f"{base}, {eq}, {limiter}, {loudnorm}"


@app.post("/api/master")
async def api_master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
    out_format: str = Form("wav"),
):
    # Extensión de salida
    out_format = (out_format or "wav").lower()
    if out_format not in ("wav", "mp3"):
        out_format = "wav"

    job_id = uuid.uuid4().hex

    # Guardar input con su extensión real si viene
    in_ext = Path(file.filename or "").suffix.lower() or ".wav"
    in_path = TMP_DIR / f"in_{job_id}{in_ext}"
    out_path = TMP_DIR / f"master_{job_id}.{out_format}"

    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    chain = build_chain(preset, intensity)

    # Params salida
    if out_format == "wav":
        out_args = ["-c:a", "pcm_s16le"]
        mime = "audio/wav"
        dl_name = "warmaster_master.wav"
    else:
        out_args = ["-c:a", "libmp3lame", "-b:a", "320k"]
        mime = "audio/mpeg"
        dl_name = "warmaster_master.mp3"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-vn",
        "-af", chain,
        *out_args,
        str(out_path)
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        # Devuelve un error legible
        err = e.stderr.decode("utf-8", errors="ignore")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "FFmpeg falló", "details": err[-2000:]},
        )
    finally:
        # Limpia input
        try:
            os.remove(in_path)
        except Exception:
            pass

    return FileResponse(
        path=str(out_path),
        media_type=mime,
        filename=dl_name,
    )


# ✅ IMPORTANTE: montar StaticFiles al FINAL para no “atrapar” /api/*
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")

