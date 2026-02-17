import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask


# =========================
# CONFIG
# =========================

MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_DURATION_SECONDS = 6 * 60  # 6 minutos

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

# mantener masters para re-descarga
KEEP_MASTERS_HOURS = 24

app = FastAPI()

# =========================
# JOB STORAGE (MEMORIA)
# =========================
jobs: Dict[str, dict] = {}

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# UTILS
# =========================
def cleanup_files(*paths: Path) -> None:
    for p in paths:
        try:
            if p and p.exists():
                p.unlink()
        except Exception:
            pass


def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg error:\n{proc.stderr[-8000:]}"
        )


def safe_filename(name: str) -> str:
    clean = "".join(c for c in (name or "") if c.isalnum() or c in "._-").strip("._-")
    return clean or "audio.wav"


def get_audio_duration_seconds(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="No se pudo analizar duración.")

    return float(proc.stdout.strip())


def db_to_lin(db: float) -> float:
    return 10 ** (db / 20.0)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def purge_old_jobs():
    cutoff = datetime.utcnow() - timedelta(hours=KEEP_MASTERS_HOURS)
    to_delete = []
    for jid, job in jobs.items():
        try:
            created = datetime.fromisoformat(job.get("created_at"))
        except Exception:
            continue
        if created < cutoff:
            to_delete.append(jid)

    for jid in to_delete:
        job = jobs.get(jid, {})
        out_path = TMP_DIR / job.get("out_file", f"master_{jid}.wav")
        in_path = TMP_DIR / job.get("in_file", f"in_{jid}.wav")
        cleanup_files(out_path, in_path)
        jobs.pop(jid, None)


def build_ffmpeg_filters(
    preset: str,
    intensity: int,
    k_low: float,
    k_mid: float,
    k_pres: float,
    k_air: float,
    k_glue: float,
    k_width: float,
    k_sat: float,
    k_out: float,
) -> str:
    intensity = int(clamp(intensity, 0, 100))

    k_low  = float(clamp(k_low,  -12, 12))
    k_mid  = float(clamp(k_mid,  -12, 12))
    k_pres = float(clamp(k_pres, -12, 12))
    k_air  = float(clamp(k_air,  -12, 12))

    k_glue  = float(clamp(k_glue,  0, 100))
    k_width = float(clamp(k_width, 50, 150))
    k_sat   = float(clamp(k_sat,   0, 100))
    k_out   = float(clamp(k_out,  -12, 6))

    preset = (preset or "clean").lower()

    # Base master chain (tu lógica original)
    thr_db = -18.0 - (intensity * 0.10)
    ratio = 2.0 + (intensity * 0.04)
    makeup = 2.0 + (intensity * 0.06)

    if preset == "club":
        base_eq = "bass=g=4:f=90,treble=g=2:f=9000"
    elif preset == "warm":
        base_eq = "bass=g=3:f=160,treble=g=-2:f=4500"
    elif preset == "bright":
        base_eq = "bass=g=-1:f=120,treble=g=4:f=8500"
    elif preset == "heavy":
        base_eq = "bass=g=5:f=90,treble=g=2:f=3500"
    else:
        base_eq = "bass=g=2:f=120,treble=g=1:f=8000"

    base_comp = (
        f"acompressor=threshold={db_to_lin(thr_db):.6f}:ratio={ratio:.3f}:attack=12:release=120:"
        f"makeup={makeup:.3f}:knee=2:detection=peak"
    )

    # Knob EQ (sobre el preset)
    knob_eq = (
        f"bass=g={k_low:.2f}:f=120,"
        f"equalizer=f=630:width_type=o:width=1.0:g={k_mid:.2f},"
        f"equalizer=f=1760:width_type=o:width=1.0:g={k_pres:.2f},"
        f"treble=g={k_air:.2f}:f=8500"
    )

    # GLUE 0..100
    gP = k_glue / 100.0
    glue_thr_db = -10.0 - (gP * 18.0)
    glue_ratio = 1.5 + (gP * 4.5)
    glue_attack = 12.0 - (gP * 7.0)
    glue_release = 200.0 + (gP * 100.0)
    glue_makeup = 0.0 + (gP * 6.0)

    glue = (
        f"acompressor=threshold={db_to_lin(glue_thr_db):.6f}:ratio={glue_ratio:.3f}:"
        f"attack={glue_attack:.2f}:release={glue_release:.2f}:knee=2:makeup={glue_makeup:.2f}:detection=peak"
    )

    # WIDTH
    width = f"stereotools=mlev=1:slev={(k_width/100.0):.3f}"

    # SAT (softclip)
    sP = k_sat / 100.0
    soft_thr = 1.0 - (sP * 0.35)
    sat = f"asoftclip=type=tanh:threshold={soft_thr:.3f}"

    # OUTPUT
    out_gain = db_to_lin(k_out)
    out = f"volume={out_gain:.6f}"

    # limiter final
    limiter = "alimiter=limit=-1.0dB"

    return ",".join([base_eq, knob_eq, base_comp, glue, width, sat, out, limiter])


# =========================
# ENDPOINTS
# =========================

@app.get("/api/health")
def health():
    purge_old_jobs()
    return {"ok": True}


@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),

    # knobs desde master.html
    k_low: float = Form(0.0),
    k_mid: float = Form(0.0),
    k_pres: float = Form(0.0),
    k_air: float = Form(0.0),
    k_glue: float = Form(0.0),
    k_width: float = Form(100.0),
    k_sat: float = Form(0.0),
    k_out: float = Form(0.0),
):
    purge_old_jobs()

    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    job_id = uuid.uuid4().hex[:8]
    safe_name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{job_id}_{safe_name}"
    out_path = TMP_DIR / f"master_{job_id}.wav"

    jobs[job_id] = {
        "status": "processing",
        "filename": safe_name,
        "preset": preset,
        "intensity": intensity,
        "created_at": datetime.utcnow().isoformat(),
        "in_file": in_path.name,
        "out_file": out_path.name,
        "knobs": {
            "low": k_low, "mid": k_mid, "pres": k_pres, "air": k_air,
            "glue": k_glue, "width": k_width, "sat": k_sat, "out": k_out
        }
    }

    try:
        with in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    if in_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        cleanup_files(in_path)
        jobs[job_id]["status"] = "error"
        raise HTTPException(status_code=400, detail="Supera 100MB.")

    duration = get_audio_duration_seconds(in_path)
    if duration > MAX_DURATION_SECONDS:
        cleanup_files(in_path)
        jobs[job_id]["status"] = "error"
        raise HTTPException(status_code=400, detail="Supera 6 minutos.")

    filters = build_ffmpeg_filters(
        preset=preset,
        intensity=intensity,
        k_low=k_low, k_mid=k_mid, k_pres=k_pres, k_air=k_air,
        k_glue=k_glue, k_width=k_width, k_sat=k_sat, k_out=k_out
    )

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(in_path),
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]

    try:
        run_ffmpeg(cmd)
    except Exception:
        jobs[job_id]["status"] = "error"
        cleanup_files(in_path, out_path)
        raise

    if not out_path.exists() or out_path.stat().st_size < 1024:
        jobs[job_id]["status"] = "error"
        cleanup_files(in_path, out_path)
        raise HTTPException(status_code=500, detail="Master vacío.")

    jobs[job_id]["status"] = "done"
    jobs[job_id]["download"] = f"/download/{job_id}"
    jobs[job_id]["duration_sec"] = duration

    headers = {"X-Master-Id": job_id}

    # SOLO borramos el input para ahorrar espacio; el output queda para descarga/preview
    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers=headers,
        background=BackgroundTask(cleanup_files, in_path),
    )


@app.get("/api/master/preview")
def master_preview(
    master_id: str = Query(...),
    seconds: int = Query(30, ge=5, le=60)
):
    purge_old_jobs()

    job = jobs.get(master_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Master no disponible.")

    src_path = TMP_DIR / job.get("out_file", f"master_{master_id}.wav")
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    prev_path = TMP_DIR / f"preview_{master_id}.wav"

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(src_path),
        "-t", str(seconds),
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(prev_path)
    ]
    run_ffmpeg(cmd)

    return FileResponse(
        path=str(prev_path),
        media_type="audio/wav",
        filename=f"warmaster_preview_{seconds}s.wav",
        background=BackgroundTask(cleanup_files, prev_path),
    )


@app.get("/download/{job_id}")
def download_job(job_id: str):
    purge_old_jobs()

    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Archivo no disponible.")

    out_path = TMP_DIR / job.get("out_file", f"master_{job_id}.wav")
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav"
    )


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
