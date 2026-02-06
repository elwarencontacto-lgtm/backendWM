from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import subprocess
import uuid
import os
import json
import re

app = FastAPI()

# ✅ CORS (para GitHub Pages)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # luego lo puedes restringir a tu dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TMP_DIR = "tmp"
os.makedirs(TMP_DIR, exist_ok=True)

def run_capture(cmd: list[str]) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])
    return p.stderr + "\n" + (p.stdout or "")

def detect_volume(input_path: str) -> dict:
    """
    Analiza volumen con ffmpeg volumedetect.
    Devuelve mean_volume y max_volume en dB (ej: -18.3)
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", input_path,
        "-af", "volumedetect",
        "-f", "null", "-"
    ]
    out = run_capture(cmd)

    mean_m = re.search(r"mean_volume:\s*(-?\d+(\.\d+)?)\s*dB", out)
    max_m  = re.search(r"max_volume:\s*(-?\d+(\.\d+)?)\s*dB", out)

    mean_db = float(mean_m.group(1)) if mean_m else None
    max_db  = float(max_m.group(1)) if max_m else None

    return {"mean_db": mean_db, "max_db": max_db}

def choose_mastering(mean_db, max_db):
    """
    “IA simple”: decide intensidad y target según el audio.
    """
    # Defaults
    intensity = "medium"
    target = "streaming"  # -14
    width = "normal"

    # Si no hay datos, devolvemos defaults
    if mean_db is None or max_db is None:
        return intensity, target, width

    # Si el audio ya viene muy alto y cerca del 0 dB, usamos más suave
    if max_db > -1.5:
        intensity = "soft"
        target = "streaming"
        width = "normal"
    # Si el audio viene muy bajo, lo empujamos más
    elif mean_db < -23:
        intensity = "hard"
        target = "radio"   # -11 aprox
        width = "normal"
    # Medio-bajo: un poco más fuerte que streaming
    elif mean_db < -19:
        intensity = "medium"
        target = "radio"
        width = "normal"
    else:
        intensity = "medium"
        target = "streaming"
        width = "normal"

    return intensity, target, width

def build_filter_chain(intensity: str, target: str, width: str):
    # Targets LUFS
    target_lufs = {"streaming": -14, "radio": -11, "club": -9}.get(target, -14)

    # Comp/limit según intensidad
    if intensity == "soft":
        comp = "compand=attacks=0.02:decays=0.15:points=-90/-90|-18/-18|-10/-8|-3/-3|0/-1"
        limit = "alimiter=limit=-1.5dB:level=true"
    elif intensity == "hard":
        comp = "compand=attacks=0.01:decays=0.12:points=-90/-90|-24/-24|-14/-10|-6/-4|0/-1"
        limit = "alimiter=limit=-0.8dB:level=true"
    else:
        comp = "compand=attacks=0.015:decays=0.13:points=-90/-90|-20/-20|-12/-9|-4/-3|0/-1"
        limit = "alimiter=limit=-1.0dB:level=true"

    # Stereo width
    width_amt = {"narrow": 0.7, "normal": 1.0, "wide": 1.25}.get(width, 1.0)
    stereo = f"stereotools=mlev=1:slev=1:phase=0:balance=0:mode=lr:width={width_amt}"

    # Loudness normalization
    loud = f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11"

    return ",".join([comp, stereo, loud, limit])

@app.get("/")
def root():
    return {"ok": True, "service": "master-backend"}

@app.post("/export-wav")
async def export_wav(audio: UploadFile = File(...), settings: str = Form("{}")):
    uid = str(uuid.uuid4())
    input_path = os.path.join(TMP_DIR, f"{uid}_in")
    output_path = os.path.join(TMP_DIR, f"{uid}_master.wav")

    try:
        # Guardar archivo subido
        data = await audio.read()
        with open(input_path, "wb") as f:
            f.write(data)

        # Leer settings (si vienen)
        cfg = json.loads(settings or "{}")

        # Si el usuario manda intensity/target/width, se usan.
        # Si NO manda, activamos “IA auto”.
        intensity = cfg.get("intensity")
        target = cfg.get("target")
        width = cfg.get("width")

        # AUTO: si falta algo, lo decidimos con análisis
        if not intensity or not target or not width:
            vol = detect_volume(input_path)
            auto_intensity, auto_target, auto_width = choose_mastering(vol["mean_db"], vol["max_db"])

            intensity = intensity or auto_intensity
            target = target or auto_target
            width = width or auto_width

        filter_chain = build_filter_chain(intensity, target, width)

        # Export WAV real (44.1k, stereo, 16-bit)
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vn",
            "-af", filter_chain,
            "-ar", "44100",
            "-ac", "2",
            "-sample_fmt", "s16",
            output_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        return FileResponse(output_path, media_type="audio/wav", filename="master.wav")

    except subprocess.CalledProcessError as e:
        return JSONResponse(status_code=500, content={"error": "FFmpeg failed", "details": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "Server error", "details": str(e)})
    finally:
        # limpieza básica
        try:
            if os.path.exists(input_path):
                os.remove(input_path)
        except:
            pass
        # output lo borraremos después de servir; si quieres, lo borramos en otro endpoint o cron.
        
