import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

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

app = FastAPI()

# =========================
# STORAGE (MEMORIA)
# =========================
# Importante: Render puede reiniciar y esto se pierde (es ok para beta).
masters: Dict[str, Dict[str, Any]] = {}  # master_id -> metadata


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
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
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


def db_to_gain(db: float) -> float:
    # gain = 10^(db/20)
    return 10 ** (db / 20.0)


def preset_chain(
    preset: str,
    intensity: Any,
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
    Devuelve cadena de filtros FFmpeg -af

    FIX PRINCIPAL (tu error):
    - 'makeup' del acompressor SIEMPRE en rango [1..64]
    """

    # intensity robusto
    intensity_i = clamp_int(intensity, 0, 100, 55)

    thr = -18.0 - (intensity_i * 0.10)
    ratio = 2.0 + (intensity_i * 0.04)

    # ✅ FIX: makeup siempre válido [1..64]
    makeup = 2.0 + (intensity_i * 0.06)
    if makeup != makeup:
        makeup = 2.0
    makeup = max(1.0, min(64.0, makeup))

    preset = (preset or "clean").lower()

    # EQ base por preset (simple y seguro)
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

    # ===== KNOBS (aplican al audio final) =====
    # EQ Live: usamos equalizer (peaking/shelf)
    # Rango knobs ya viene clamp en endpoint:
    # low/mid/pres/air: -12..+12 dB
    eq_live = (
        f"equalizer=f=120:width_type=h:width=1:g={k_low},"
        f"equalizer=f=630:width_type=h:width=1:g={k_mid},"
        f"equalizer=f=1760:width_type=h:width=1:g={k_pres},"
        f"equalizer=f=8500:width_type=h:width=1:g={k_air}"
    )

    # Glue (0..100): ajusta compresión extra (suave)
    # (no rompe si está en 0)
    glue_p = max(0.0, min(100.0, k_glue)) / 100.0
    glue_thr = -12.0 - glue_p * 18.0
    glue_ratio = 1.2 + glue_p * 3.8
    glue_attack = 0.012 - glue_p * 0.007
    glue_release = 0.20 + glue_p * 0.10
    glue_comp = (
        f"acompressor=threshold={glue_thr}dB:ratio={glue_ratio}:attack={glue_attack}:release={glue_release}:makeup=1"
    )

    # Width (50..150): usamos stereowiden (seguro). amount = 0..1 aprox
    width_amt = max(50.0, min(150.0, k_width))
    # map 50..150 -> 0.0..1.0
    widen = (width_amt - 50.0) / 100.0
    width_fx = f"stereowiden=amount={widen}"

    # Saturation (0..100): drive con volume + asoftclip (type=tanh es válido)
    sat_p = max(0.0, min(100.0, k_sat)) / 100.0
    drive_db = sat_p * 9.0  # hasta +9dB
    back_db = -sat_p * 6.0  # compensa un poco
    sat_fx = f"volume={drive_db}dB,asoftclip=type=tanh,volume={back_db}dB"

    # Output (dB)
    out_fx = f"volume={k_out}dB"

    # Limiter final
    limiter = "alimiter=limit=-1.0dB"

    # Comp base (tu cadena original) + live knobs
    return (
        f"{eq_base},"
        f"{eq_live},"
        f"acompressor=threshold={thr}dB:ratio={ratio}:attack=12:release=120:makeup={makeup},"
        f"{glue_comp},"
        f"{width_fx},"
        f"{sat_fx},"
        f"{out_fx},"
        f"{limiter}"
    )


# =========================
# ENDPOINTS
# =========================
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me():
    # beta: fijo. Luego lo haces real (auth/pagos)
    return {"plan": "FREE"}


@app.get("/api/masters")
def list_masters():
    # Devuelve array tal cual lo espera dashboard.html
    items = []
    for mid, m in masters.items():
        items.append({
            "id": mid,
            "title": m.get("title") or f"Master {mid}",
            "quality": m.get("quality", "PLUS"),
            "preset": m.get("preset", "clean"),
            "intensity": m.get("intensity", 55),
            "created_at": m.get("created_at"),
        })
    # más recientes primero
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

    # stream inline
    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers={"Content-Disposition": 'inline; filename="warmaster_master.wav"'}
    )


@app.get("/download/{master_id}")
def download_master(master_id: str):
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    out_path = TMP_DIR / f"master_{master_id}.wav"
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav"
    )


@app.get("/api/master/preview")
def preview_master(
    master_id: str = Query(...),
    seconds: int = Query(30, ge=5, le=60),
):
    """
    FREE: descarga preview 30s (o el que pases)
    """
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    in_path = TMP_DIR / f"master_{master_id}.wav"
    if not in_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

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

    return FileResponse(
        path=str(prev_path),
        media_type="audio/wav",
        filename=f"warmaster_preview_{seconds}s.wav",
        background=BackgroundTask(cleanup_files, prev_path),
    )


@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),

    # knobs (llegan desde tu master.html)
    k_low: float = Form(0.0),
    k_mid: float = Form(0.0),
    k_pres: float = Form(0.0),
    k_air: float = Form(0.0),
    k_glue: float = Form(0.0),
    k_width: float = Form(100.0),
    k_sat: float = Form(0.0),
    k_out: float = Form(0.0),

    # compat (no rompe)
    requested_quality: Optional[str] = Form(None),
    target: Optional[str] = Form(None),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    master_id = uuid.uuid4().hex[:8]
    name = safe_filename(file.filename)
    in_path = TMP_DIR / f"in_{master_id}_{name}"
    out_path = TMP_DIR / f"master_{master_id}.wav"

    # Guardamos metadata (para dashboard)
    masters[master_id] = {
        "id": master_id,
        "title": name,
        "preset": preset,
        "intensity": int(clamp_int(intensity, 0, 100, 55)),
        "quality": (requested_quality or "PLUS").upper(),  # beta
        "created_at": datetime.utcnow().isoformat(),
        "knobs": {
            "low": k_low, "mid": k_mid, "pres": k_pres, "air": k_air,
            "glue": k_glue, "width": k_width, "sat": k_sat, "out": k_out
        }
    }

    # Guardar archivo
    try:
        with in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # VALIDAR TAMAÑO
    if in_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="Supera 100MB.")

    # VALIDAR DURACIÓN
    duration = get_audio_duration_seconds(in_path)
    if duration > MAX_DURATION_SECONDS:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="Supera 6 minutos.")

    # Clamp knobs (seguro)
    k_low = clamp_float(k_low, -12, 12, 0.0)
    k_mid = clamp_float(k_mid, -12, 12, 0.0)
    k_pres = clamp_float(k_pres, -12, 12, 0.0)
    k_air = clamp_float(k_air, -12, 12, 0.0)
    k_glue = clamp_float(k_glue, 0, 100, 0.0)
    k_width = clamp_float(k_width, 50, 150, 100.0)
    k_sat = clamp_float(k_sat, 0, 100, 0.0)
    k_out = clamp_float(k_out, -12, 6, 0.0)

    filters = preset_chain(
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

    # Ejecutar ffmpeg
    try:
        run_cmd(cmd)
    except Exception:
        # si falla, limpiamos input y output (output puede no existir)
        cleanup_files(in_path, out_path)
        # dejamos metadata igual (para debug), pero si quieres puedes borrar masters[master_id]
        raise

    # Validación salida
    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, out_path)
        raise HTTPException(status_code=500, detail="Master vacío.")

    # Limpia SOLO el input. (No borres el master, porque dashboard/descarga lo necesita)
    cleanup_files(in_path)

    # Respuesta: devolvemos el master + header para que tu front lo capture
    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers={"X-Master-Id": master_id}
    )


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
