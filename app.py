import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# =========================
# CONFIG
# =========================
MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_DURATION_SECONDS = 6 * 60  # 6 minutos
FREE_PREVIEW_SECONDS = 30

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

app = FastAPI()

# =========================
# STORAGE (MEMORIA)
# =========================
masters: Dict[str, Dict[str, Any]] = {}  # master_id -> metadata

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,          # ✅ con "*" debe ser False
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Master-Id"],   # ✅ frontend puede leerlo si fuese cross-origin
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


def run_cmd(cmd: list[str]) -> None:
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
    clean = "".join(c for c in (name or "") if c.isalnum() or c in "._- ").strip().strip("._-")
    clean = clean.replace(" ", "_")
    return clean or "audio"


def get_audio_duration_seconds(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="No se pudo analizar duración.")
    try:
        return float(proc.stdout.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Duración inválida.")


def clamp_float(x: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(x)
        if v != v:  # NaN
            return default
        return max(lo, min(hi, v))
    except Exception:
        return default


def clamp_int(x: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(float(x))
        return max(lo, min(hi, v))
    except Exception:
        return default


def normalize_quality(q: Optional[str]) -> str:
    q = (q or "FREE").strip().upper()
    if q not in ("FREE", "PLUS", "PRO"):
        return "FREE"
    return q
def preset_chain(
    preset: str,
    intensity_val: int,
    k_low: float,
    k_mid: float,
    k_pres: float,
    k_air: float,
    k_glue: float,
    k_width: float,
    k_sat: float,
    k_out: float,
) -> str:
    """
    Cadena de filtros FFmpeg para -af
    ✅ robusto para mono: fuerza stereo antes del pan
    """
    intensity_i = clamp_int(intensity_val, 0, 100, 55)

    thr = -18.0 - (intensity_i * 0.10)
    ratio = 2.0 + (intensity_i * 0.04)

    makeup = 2.0 + (intensity_i * 0.06)
    if makeup != makeup:
        makeup = 2.0
    makeup = max(1.0, min(64.0, makeup))

    preset = (preset or "streaming").lower()

    # EQ base por preset
    if preset == "club":
        eq_base = "bass=g=4:f=90,treble=g=2:f=9000"
    elif preset == "warm":
        eq_base = "bass=g=3:f=160,treble=g=-2:f=4500"
    elif preset == "bright":
        eq_base = "bass=g=-1:f=120,treble=g=4:f=8500"
    elif preset == "heavy":
        eq_base = "bass=g=5:f=90,treble=g=2:f=3500"
    else:
        eq_base = "bass=g=2:f=120,treble=g=1:f=8000"

    # EQ Live 4 bandas
    eq_live = (
        f"equalizer=f=120:width_type=h:width=1:g={k_low},"
        f"equalizer=f=630:width_type=h:width=1:g={k_mid},"
        f"equalizer=f=1760:width_type=h:width=1:g={k_pres},"
        f"equalizer=f=8500:width_type=h:width=1:g={k_air}"
    )

    # Glue 0..100
    glue_p = max(0.0, min(100.0, k_glue)) / 100.0
    glue_thr = -12.0 - glue_p * 18.0
    glue_ratio = 1.2 + glue_p * 3.8
    glue_attack = max(0.001, 0.012 - glue_p * 0.007)
    glue_release = 0.20 + glue_p * 0.10
    glue_comp = (
        f"acompressor=threshold={glue_thr}dB:"
        f"ratio={glue_ratio}:attack={glue_attack}:release={glue_release}:makeup=1"
    )

    # WIDTH (50..150) + robusto para MONO
    k = max(50.0, min(150.0, k_width)) / 100.0
    a = (1.0 + k) / 2.0
    b = (1.0 - k) / 2.0
    force_stereo = "aformat=channel_layouts=stereo"
    width_fx = f"pan=stereo|c0={a:.6f}*c0+{b:.6f}*c1|c1={b:.6f}*c0+{a:.6f}*c1"

    # SAT 0..100 (seguro)
    sat_p = max(0.0, min(100.0, k_sat)) / 100.0
    drive_db = sat_p * 6.0
    back_db = -sat_p * 4.0
    sat_comp_thr = -14.0 + sat_p * 6.0
    sat_comp_ratio = 1.2 + sat_p * 2.8
    sat_fx = (
        f"volume={drive_db}dB,"
        f"acompressor=threshold={sat_comp_thr}dB:ratio={sat_comp_ratio}:attack=2:release=80:makeup=1,"
        f"volume={back_db}dB"
    )

    out_fx = f"volume={k_out}dB"
    limiter = "alimiter=limit=-1.0dB"

    return (
        f"{eq_base},"
        f"{eq_live},"
        f"acompressor=threshold={thr}dB:ratio={ratio}:attack=12:release=120:makeup={makeup},"
        f"{glue_comp},"
        f"{force_stereo},"
        f"{width_fx},"
        f"{sat_fx},"
        f"{out_fx},"
        f"{limiter}"
    )


def build_preview_wav(master_id: str, seconds: int) -> Path:
    in_path = TMP_DIR / f"master_{master_id}.wav"
    if not in_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    seconds = int(max(5, min(60, seconds)))
    prev_path = TMP_DIR / f"preview_{master_id}_{seconds}.wav"

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(in_path),
        "-t", str(seconds),
        "-vn",
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(prev_path)
    ]
    run_cmd(cmd)

    if not prev_path.exists() or prev_path.stat().st_size < 1024:
        cleanup_files(prev_path)
        raise HTTPException(status_code=500, detail="Preview vacío.")
    return prev_path


def resolve_download_path(master_id: str) -> Path:
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    quality = normalize_quality(m.get("quality"))
    if quality == "FREE":
        return build_preview_wav(master_id, FREE_PREVIEW_SECONDS)

    out_path = TMP_DIR / f"master_{master_id}.wav"
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    return out_path
# =========================
# API
# =========================
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me():
    return {"plan": "FREE"}


@app.get("/api/masters")
def list_masters():
    items = []
    for mid, m in masters.items():
        items.append({
            "id": mid,
            "title": m.get("title") or f"Master {mid}",
            "quality": normalize_quality(m.get("quality", "FREE")),
            "preset": m.get("preset", "streaming"),
            "intensity": m.get("intensity", 55),
            "created_at": m.get("created_at"),
        })
    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return items


@app.get("/stream/{master_id}")
def stream_master(master_id: str):
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    out_path = TMP_DIR / f"master_{master_id}.wav"
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers={"Content-Disposition": 'inline; filename="warmaster_master.wav"'}
    )


@app.get("/download/{master_id}")
def download_master(master_id: str):
    dl_path = resolve_download_path(master_id)
    m = masters.get(master_id) or {}
    q = normalize_quality(m.get("quality"))
    fname = "warmaster_master.wav" if q in ("PLUS", "PRO") else f"warmaster_preview_{FREE_PREVIEW_SECONDS}s.wav"
    return FileResponse(path=str(dl_path), media_type="audio/wav", filename=fname)


@app.get("/api/master/preview")
def preview_master(
    master_id: str = Query(...),
    seconds: int = Query(30, ge=5, le=60),
):
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")
    prev_path = build_preview_wav(master_id, seconds)
    return FileResponse(path=str(prev_path), media_type="audio/wav", filename=f"warmaster_preview_{seconds}s.wav")


@app.post("/api/master")
async def master(
    file: UploadFile = File(...),

    # ✅ tu master.html manda target + intensity (strings)
    target: str = Form("streaming"),
    intensity: str = Form("balanced"),

    # knobs
    k_low: float = Form(0.0),
    k_mid: float = Form(0.0),
    k_pres: float = Form(0.0),
    k_air: float = Form(0.0),
    k_glue: float = Form(0.0),
    k_width: float = Form(100.0),
    k_sat: float = Form(0.0),
    k_out: float = Form(0.0),

    # compat
    requested_quality: Optional[str] = Form(None),
    preset: Optional[str] = Form(None),

    # ✅ default JSON (evita "Failed to fetch" en WAV largos)
    return_blob: int = Query(0, ge=0, le=1),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    # intensity mapping
    intensity_map = {"soft": 35, "balanced": 55, "aggressive": 80}
    intensity_str = (intensity or "balanced").strip().lower()
    intensity_val = intensity_map.get(intensity_str)
    if intensity_val is None:
        try:
            intensity_val = int(float(intensity_str))
        except Exception:
            intensity_val = 55
    intensity_val = max(0, min(100, intensity_val))

    master_id = uuid.uuid4().hex[:8]
    name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{master_id}_{name}"
    out_path = TMP_DIR / f"master_{master_id}.wav"

    rq = normalize_quality(requested_quality)
    preset_use = (preset or target or "streaming").strip().lower()

    masters[master_id] = {
        "id": master_id,
        "title": name,
        "preset": preset_use,
        "intensity": int(intensity_val),
        "quality": rq,
        "created_at": datetime.utcnow().isoformat(),
        "knobs": {
            "low": k_low, "mid": k_mid, "pres": k_pres, "air": k_air,
            "glue": k_glue, "width": k_width, "sat": k_sat, "out": k_out
        }
    }

    # guardar archivo
    try:
        with in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # validar tamaño
    if in_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="Supera 100MB.")

    # validar duración
    duration = get_audio_duration_seconds(in_path)
    if duration > MAX_DURATION_SECONDS:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="Supera 6 minutos.")

    # clamp knobs
    k_low = clamp_float(k_low, -12, 12, 0.0)
    k_mid = clamp_float(k_mid, -12, 12, 0.0)
    k_pres = clamp_float(k_pres, -12, 12, 0.0)
    k_air = clamp_float(k_air, -12, 12, 0.0)
    k_glue = clamp_float(k_glue, 0, 100, 0.0)
    k_width = clamp_float(k_width, 50, 150, 100.0)
    k_sat = clamp_float(k_sat, 0, 100, 0.0)
    k_out = clamp_float(k_out, -12, 6, 0.0)

    filters = preset_chain(
        preset=preset_use,
        intensity_val=intensity_val,
        k_low=k_low,
        k_mid=k_mid,
        k_pres=k_pres,
        k_air=k_air,
        k_glue=k_glue,
        k_width=k_width,
        k_sat=k_sat,
        k_out=k_out,
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
        run_cmd(cmd)
    except Exception:
        cleanup_files(in_path, out_path)
        raise

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, out_path)
        raise HTTPException(status_code=500, detail="Master vacío.")

    cleanup_files(in_path)

    # ✅ por defecto: JSON rápido (no se cae en audios largos)
    if return_blob == 1:
        return FileResponse(
            path=str(out_path),
            media_type="audio/wav",
            filename="warmaster_master.wav",
            headers={"X-Master-Id": master_id}
        )

    return JSONResponse(
        content={"ok": True, "master_id": master_id},
        headers={"X-Master-Id": master_id}
    )


# =========================
# FRONTEND (NO pisa /api)
# =========================
app.mount("/public", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")

@app.get("/")
def root():
    return RedirectResponse(url="/public/index.html")

@app.get("/index.html")
def index():
    return RedirectResponse(url="/public/index.html")

@app.get("/master.html")
def master_html():
    return RedirectResponse(url="/public/master.html")

@app.get("/dashboard.html")
def dashboard_html():
    return RedirectResponse(url="/public/dashboard.html")
