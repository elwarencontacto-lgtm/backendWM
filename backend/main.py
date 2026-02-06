from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, os, json, re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TMP = "tmp"
os.makedirs(TMP, exist_ok=True)

@app.get("/")
def root():
    return {"ok": True, "service": "master-backend"}

def safe_float(v, d):
    try:
        return float(v)
    except:
        return d

def build_eq(bands):
    filters = []
    if not isinstance(bands, list):
        return filters

    for b in bands[:6]:
        freq = safe_float(b.get("freq"), 1000)
        gain = safe_float(b.get("gain"), 0)
        q    = safe_float(b.get("q"), 1.0)

        # límites seguros
        freq = max(20, min(20000, freq))
        gain = max(-18, min(18, gain))
        q    = max(0.3, min(10.0, q))

        filters.append(
            f"equalizer=f={freq}:width_type=q:width={q}:g={gain}"
        )

    return filters

def chain(intensity, target, width, bands):
    eq = build_eq(bands)

    if intensity == "hard":
        comp = "compand=attacks=0.01:decays=0.12:points=-90/-90|-24/-24|-12/-8|-6/-3|0/-1"
        limit = "alimiter=limit=-0.8dB"
    elif intensity == "soft":
        comp = "compand=attacks=0.02:decays=0.2:points=-90/-90|-18/-18|-8/-6|-3/-2|0/-1"
        limit = "alimiter=limit=-1.5dB"
    else:
        comp = "compand=attacks=0.015:decays=0.15:points=-90/-90|-20/-20|-10/-7|-4/-2|0/-1"
        limit = "alimiter=limit=-1.0dB"

    stereo = {
        "narrow": "stereotools=width=0.8",
        "wide": "stereotools=width=1.25"
    }.get(width, "stereotools=width=1.0")

    loud = {
        "club": "loudnorm=I=-9:TP=-1.0",
        "radio": "loudnorm=I=-11:TP=-1.0"
    }.get(target, "loudnorm=I=-14:TP=-1.0")

    return ",".join(eq + [comp, stereo, loud, limit])

@app.post("/export-wav")
async def export(audio: UploadFile = File(...), settings: str = Form("{}")):
    uid = str(uuid.uuid4())
    inp = f"{TMP}/{uid}_in"
    out = f"{TMP}/{uid}_master.wav"

    try:
        with open(inp, "wb") as f:
            f.write(await audio.read())

        cfg = json.loads(settings or "{}")

        intensity = cfg.get("intensity", "medium")
        target    = cfg.get("target", "streaming")
        width     = cfg.get("width", "normal")
        bands     = cfg.get("bands", [])

        af = chain(intensity, target, width, bands)

        cmd = [
            "ffmpeg", "-y",
            "-i", inp,
            "-vn",
            "-af", af,
            "-ar", "44100",
            "-ac", "2",
            "-sample_fmt", "s16",
            out
        ]

        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        return FileResponse(out, media_type="audio/wav", filename="master.wav")

    except subprocess.CalledProcessError as e:
        return JSONResponse(status_code=500, content={
            "error": "FFmpeg failed",
            "details": e.stderr.decode() if hasattr(e.stderr, "decode") else str(e)
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        for f in (inp, out):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except:
                pass
