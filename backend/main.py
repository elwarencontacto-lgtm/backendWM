import os, json, tempfile, subprocess
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])
    return p

@app.get("/")
def home():
    return {"ok": True}

@app.post("/export-wav")
async def export_wav(audio: UploadFile = File(...), settings: str = Form("{}")):
    s = json.loads(settings or "{}")

    intensity = s.get("intensity", "medium")
    target = s.get("target", "streaming")
    width = s.get("width", "normal")
    bands = s.get("bands", [])

    target_lufs = {"streaming": -14, "radio": -11, "club": -9}.get(target, -14)

    if intensity == "soft":
        comp = "compand=attacks=0.02:decays=0.15:points=-90/-90|-18/-18|-10/-8|-3/-3|0/-1"
        limit = "alimiter=limit=-1.5dB:level=true"
    elif intensity == "hard":
        comp = "compand=attacks=0.01:decays=0.12:points=-90/-90|-24/-24|-14/-10|-6/-4|0/-1"
        limit = "alimiter=limit=-0.8dB:level=true"
    else:
        comp = "compand=attacks=0.015:decays=0.13:points=-90/-90|-20/-20|-12/-9|-4/-3|0/-1"
        limit = "alimiter=limit=-1.0dB:level=true"

    width_amt = {"narrow": 0.7, "normal": 1.0, "wide": 1.25}.get(width, 1.0)
    stereo = f"stereotools=mlev=1:slev=1:phase=0:balance=0:mode=lr:width={width_amt}"

    eq_filters = []
    for b in bands[:6]:
        if not b.get("on", True):
            continue
        f = float(b.get("freq", 1000))
        g = float(b.get("gain", 0))
        q = float(b.get("q", 1.0))
        w = max(0.1, min(4.0, 1.6 / max(0.2, q)))
        eq_filters.append(f"equalizer=f={f}:t=q:w={w}:g={g}")

    loud = f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11"

    filter_chain = ",".join(eq_filters + [comp, stereo, loud, limit])

    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in")
        out_path = os.path.join(td, "master.wav")

        data = await audio.read()
        with open(in_path, "wb") as f:
            f.write(data)

        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-vn",
            "-af", filter_chain,
            "-ar", "44100",
            "-ac", "2",
            "-sample_fmt", "s16",
            out_path
        ]
        run(cmd)

        return FileResponse(out_path, media_type="audio/wav", filename="master.wav")
