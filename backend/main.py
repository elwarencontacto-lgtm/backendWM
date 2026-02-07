from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import subprocess, uuid, os, json

app = FastAPI()

# ✅ Cambia SOLO si tu dominio de GitHub Pages es distinto
ALLOWED_ORIGINS = [
    "https://elwarencontacto-lgtm.github.io",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

TMP = "tmp"
os.makedirs(TMP, exist_ok=True)

@app.get("/")
def root():
    return {"ok": True, "service": "master-backend"}

@app.get("/health")
def health():
    return {"ok": True}

def safe(v, d):
    try: return float(v)
    except: return d

def build_eq(bands):
    filters = []
    if not isinstance(bands, list):
        return filters
    for b in bands[:6]:
        f = safe(b.get("freq"), 1000)
        g = safe(b.get("gain"), 0)
        q = safe(b.get("q"), 1.0)

        f = max(30, min(16000, f))
        g = max(-18, min(18, g))
        q = max(0.3, min(10, q))

        # ✅ BIQUAD (estable en Render)
        filters.append(f"biquad=type=peaking:frequency={f}:gain={g}:width={q}")
    return filters

def chain(cfg):
    eq = build_eq(cfg.get("bands", []))

    intensity = cfg.get("intensity", "medium")
    target = cfg.get("target", "streaming")
    width = cfg.get("width", "normal")

    comp = {
        "soft":   "acompressor=threshold=-18dB:ratio=2",
        "hard":   "acompressor=threshold=-22dB:ratio=4",
    }.get(intensity, "acompressor=threshold=-20dB:ratio=3")

    stereo = {
        "narrow": "stereotools=width=0.8",
        "wide":   "stereotools=width=1.25"
    }.get(width, "stereotools=width=1.0")

    loud = {
        "club":  "loudnorm=I=-9:TP=-1",
        "radio": "loudnorm=I=-11:TP=-1"
    }.get(target, "loudnorm=I=-14:TP=-1")

    limit = "alimiter=limit=-1.0dB"

    return ",".join(eq + [comp, stereo, loud, limit])

@app.post("/export-wav")
async def export_wav(audio: UploadFile = File(...), settings: str = Form("{}")):
    uid = str(uuid.uuid4())
    inp = f"{TMP}/{uid}_in"
    out = f"{TMP}/{uid}.wav"

    try:
        with open(inp, "wb") as f:
            f.write(await audio.read())

        cfg = json.loads(settings or "{}")
        af = chain(cfg)

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
        return JSONResponse(500, {
            "error": "FFmpeg failed",
            "details": e.stderr.decode() if hasattr(e.stderr, "decode") else str(e)
        })
    except Exception as e:
        return JSONResponse(500, {"error": "Server error", "details": str(e)})
    finally:
        for f in (inp, out):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except:
                pass
