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
    allow_origins=["*"],  # luego puedes restringir a tu dominio
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
    """Analiza volumen con ffmpeg volumedetect."""
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
    """IA simple: decide intensidad/target según el audio."""
    intensity = "medium"
    target = "streaming"
    width = "normal"

    if mean_db is None or max_db is None:
        return intensity, target, width

    if max_db > -1.5:
        intensity = "soft"
        target = "streaming"
    elif mean_db < -23:
        intensity = "hard"
        target = "radio"
    elif mean_db < -19:
        intensity = "medium"
        target = "radio"
    else:
        intensity = "medium"
        target = "streaming"

    return intensity, target, width

def build_eq_filters(bands):
    """
    bands: lista de hasta 6 objetos:
      {"freq":80,"gain":2.0,"q":1.0,"on":true}
    Usa ffmpeg equalizer (peaking) aproximando Q -> w.
    """
    eq_filters = []
    if not isinstance(bands, list):
        return eq_filters

    for b in bands[:6]:
        try:
            if not b.get("on", True):
                continue
            f = float(b.get("freq", 1000))
            g = float(b.get("gain", 0))
            q = float(b.get("q", 1.0))

            # convertir Q a "w" (ancho en octavas aprox)
            w = max(0.1, min(4.0, 1.6 / max(0.2, q)))

            # rango seguro
            f = max(20.0, min(20000.0, f))
            g = max(-24.0, min(24.0, g))

            eq_filters.append(f"equalizer=f={f}:t=q:w={w}:g={g}")
        except Exception:
            continue

    return eq_filters

def build_filter_chain(intensity: str, target: str, width: str, bands):
    # Targets LUFS
    target_lufs = {"streaming": -14, "radio": -11, "club": -9}.get(target, -14)

    # EQ (6 bandas) al inicio
    eq = build_eq_filters(bands)

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

    # orden: EQ -> comp -> stereo -> loudnorm -> limiter
    return ",".join(eq + [comp, stereo, loud, limit])

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

        cfg = json.loads(settings or "{}")

        intensity = cfg.get("intensity")
        target = cfg.get("target")
        width = cfg.get("width")
        bands = cfg.get("bands", [])

        # IA: si falta intensity/target/width, el backend decide solo
        if not intensity or not target or not width:
            vol = detect_volume(input_path)
            ai_int, ai_target, ai_width = choose_mastering(vol["mean_db"], vol["max_db"])
            intensity = intensity or ai_int
            target = target or ai_target
            width = width or ai_width

        filter_chain = build_filter_chain(intensity, target, width, bands)

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
        try:
            if os.path.exists(input_path):
                os.remove(input_path)
        except:
            pass
