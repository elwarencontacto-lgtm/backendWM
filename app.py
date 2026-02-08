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
    """Cadena simple pero REAL: EQ + limitador + normalización LUFS."""
    preset = (preset or "clean").lower()

    # Clamp
    try:
        intensity = int(intensity)
    except Exception:
        intensity = 55
    intensity = max(0, min(100, intensity))

    # Base
    base = "highpass=f=30"

    # Presets simples
    if preset == "club":
        eq = "equalizer=f=80:width_type=o:width=1:g=3, equalizer=f=9000:width_type=o:width=1:g=2"
    elif preset == "warm":
        eq = "equalizer=f=200:width_type=o:width=1:g=2, equalizer=f=7000:width_type=o:width=1:g=-1.5"
    elif preset == "bright":
        eq = "equalizer=f=12000:width_type=o:width=1:g=3, equalizer=f=250:width_type=o:width=1:g=-1"
    elif preset == "heavy":
        eq = "equalizer=f=120:width_type=o:width=1:g=3, equalizer=f=3500:width_type=o:width=1:g=2"
    else:
        eq = "equalizer=f=100:width_type=o:width=1:g=1.5, equalizer=f=10000:width_type=o:width=1:g=1.0"

    limiter = "alimiter=limit=0.97:level=0.97"

    # Intensidad -> LUFS objetivo (realista para demo)
    target_lufs = -14 + (intensity / 100.0) * 6.0  # -14..-8 aprox

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

