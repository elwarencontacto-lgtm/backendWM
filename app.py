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
MAX_DURATION_SECONDS = 6 * 60
FREE_PREVIEW_SECONDS = 30

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store (beta)
masters: Dict[str, Dict[str, Any]] = {}


# =========================
# APP
# =========================
app = FastAPI()

# CORS (deja esto abierto en beta; luego lo cierras)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir /public si existe (index/master/dashboard)
if PUBLIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")


# =========================
# HELPERS
# =========================
def safe_filename(name: str) -> str:
    name = (name or "").strip()
    name = name.replace("\\", "_").replace("/", "_")
    return "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", ".", " ")).strip() or "audio"

def cleanup_files(*paths: Path):
    for p in paths:
        try:
            if p and p.exists():
                p.unlink()
        except Exception:
            pass

def run_cmd(cmd: list[str]):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise HTTPException(status_code=500, detail=f"FFmpeg error:\n{p.stderr}")
    return p

def get_audio_duration_seconds(path: Path) -> float:
    # ffprobe
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise HTTPException(status_code=400, detail="No se pudo leer duración del audio.")
    try:
        return float((p.stdout or "").strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Duración inválida.")

def clamp_int(v: int, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except Exception:
        return default
    return max(lo, min(hi, v))

def clamp_float(v: float, lo: float, hi: float, default: float) -> float:
    try:
        v = float(v)
    except Exception:
        return default
    return max(lo, min(hi, v))

def normalize_quality(q: Optional[str]) -> str:
    q = (q or "").strip().upper()
    if q not in ("FREE", "PLUS", "PRO"):
        return "FREE"
    return q

def preset_chain(
    preset: str,
    intensity: int,
    k_low: float, k_mid: float, k_pres: float, k_air: float,
    k_glue: float, k_width: float, k_sat: float, k_out: float
) -> str:
    preset = (preset or "clean").lower().strip()
    intensity = clamp_int(intensity, 0, 100, 55)

    # Base EQ por preset (muy leve)
    if preset == "warm":
        eq_base = "equalizer=f=120:t=q:w=1.1:g=1.8,equalizer=f=350:t=q:w=1.0:g=0.8,equalizer=f=9000:t=q:w=1.0:g=-0.4"
    elif preset == "bright":
        eq_base = "equalizer=f=120:t=q:w=1.1:g=-0.8,equalizer=f=3500:t=q:w=1.0:g=1.4,equalizer=f=12000:t=q:w=1.0:g=1.2"
    elif preset == "heavy":
        eq_base = "equalizer=f=80:t=q:w=1.0:g=2.2,equalizer=f=250:t=q:w=1.0:g=1.0,equalizer=f=6000:t=q:w=1.0:g=-0.8"
    elif preset == "club":
        eq_base = "equalizer=f=60:t=q:w=1.0:g=3.0,equalizer=f=9000:t=q:w=1.0:g=1.2,equalizer=f=14000:t=q:w=1.0:g=1.0"
    else:  # clean/streaming
        eq_base = "equalizer=f=110:t=q:w=1.1:g=0.6,equalizer=f=8000:t=q:w=1.0:g=0.4"

    # EQ knobs (LIVE)
    eq_live = (
        f"equalizer=f=120:t=q:w=1.0:g={k_low},"
        f"equalizer=f=650:t=q:w=1.0:g={k_mid},"
        f"equalizer=f=3500:t=q:w=1.0:g={k_pres},"
        f"equalizer=f=12000:t=q:w=1.0:g={k_air}"
    )

    # Comp según intensidad
    # intensidad 0..100 -> threshold/ratio/makeup suaves
    t = intensity / 100.0
    thr = -18.0 + t * 10.0      # -18 .. -8
    ratio = 1.6 + t * 2.0       # 1.6 .. 3.6
    makeup = 1.0 + t * 2.5      # 1.0 .. 3.5

    # Glue 0..100 (compresión suave adicional)
    glue_p = max(0.0, min(100.0, k_glue)) / 100.0
    glue_thr = -12.0 - glue_p * 18.0
    glue_ratio = 1.2 + glue_p * 3.8
    glue_attack = max(0.001, 0.012 - glue_p * 0.007)
    glue_release = 0.20 + glue_p * 0.10
    glue_comp = (
        f"acompressor=threshold={glue_thr}dB:"
        f"ratio={glue_ratio}:attack={glue_attack}:release={glue_release}:makeup=1"
    )

    # ✅ WIDTH (50..150) PAN compatible
    # L = a*L + b*R ; R = b*L + a*R
    # k = width/100
    k = max(50.0, min(150.0, k_width)) / 100.0
    a = (1.0 + k) / 2.0
    b = (1.0 - k) / 2.0
    width_fx = f"pan=stereo|c0={a:.6f}*c0+{b:.6f}*c1|c1={b:.6f}*c0+{a:.6f}*c1"

    # ✅ SAT (0..100): “densidad” segura (sin asoftclip)
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

    # Output
    out_fx = f"volume={k_out}dB"

    # Limiter final
    limiter = "alimiter=limit=-1.0dB"

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
        raise HTTPException(status_code=500, detail="Preview vacío.")
    return prev_path

def resolve_download_path(master_id: str) -> Path:
    # FREE => preview 30s; PLUS/PRO => full
    m = masters.get(master_id) or {}
    q = normalize_quality(m.get("quality"))
    if q in ("PLUS", "PRO"):
        out_path = TMP_DIR / f"master_{master_id}.wav"
        if not out_path.exists():
            raise HTTPException(status_code=404, detail="Archivo no encontrado.")
        return out_path
    # FREE
    prev_path = build_preview_wav(master_id, FREE_PREVIEW_SECONDS)
    return prev_path


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
    items = []
    for mid, m in masters.items():
        items.append({
            "id": mid,
            "title": m.get("title") or f"Master {mid}",
            "quality": normalize_quality(m.get("quality", "FREE")),
            "preset": m.get("preset", "clean"),
            "intensity": m.get("intensity", 55),
            "created_at": m.get("created_at"),
        })
    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return items

# ✅ Dashboard URL (nuevo)
@app.get("/api/masters/{master_id}/stream")
def api_stream_master(master_id: str):
    return stream_master(master_id)

# ✅ Dashboard URL (nuevo) con enforcement FREE 30s
@app.get("/api/masters/{master_id}/download")
def api_download_master(master_id: str):
    return download_master(master_id)

# LEGACY (tu master.html actual puede usar estos)
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

    # nombre según plan
    m = masters.get(master_id) or {}
    q = normalize_quality(m.get("quality"))
    fname = "warmaster_master.wav" if q in ("PLUS", "PRO") else f"warmaster_preview_{FREE_PREVIEW_SECONDS}s.wav"

    return FileResponse(
        path=str(dl_path),
        media_type="audio/wav",
        filename=fname
    )

@app.get("/api/master/preview")
def preview_master(
    master_id: str = Query(...),
    seconds: int = Query(30, ge=5, le=60),
):
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    prev_path = build_preview_wav(master_id, seconds)

    return FileResponse(
        path=str(prev_path),
        media_type="audio/wav",
        filename=f"warmaster_preview_{seconds}s.wav",
    )

@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),

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
    target: Optional[str] = Form(None),

    # ✅ Nuevo: si quieres forzar respuesta WAV en el POST (modo antiguo)
    return_blob: int = Query(0, ge=0, le=1),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    master_id = uuid.uuid4().hex[:8]
    name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{master_id}_{name}"
    out_path = TMP_DIR / f"master_{master_id}.wav"

    rq = normalize_quality(requested_quality)

    masters[master_id] = {
        "id": master_id,
        "title": name,
        "preset": preset,
        "intensity": int(clamp_int(intensity, 0, 100, 55)),
        "quality": rq,
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

    # Validar tamaño
    if in_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="Supera 100MB.")

    # Validar duración
    duration = get_audio_duration_seconds(in_path)
    if duration > MAX_DURATION_SECONDS:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="Supera 6 minutos.")

    # Clamp knobs
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

    try:
        run_cmd(cmd)
    except Exception:
        cleanup_files(in_path, out_path)
        raise

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, out_path)
        raise HTTPException(status_code=500, detail="Master vacío.")

    cleanup_files(in_path)

    # Nota: para audios largos, devolver el WAV completo en la misma respuesta puede fallar por tamaño/tiempo en proxy.
    # Por defecto devolvemos JSON + master_id, y el front reproduce/descarga vía /stream y /download.
    if return_blob == 1:
        return FileResponse(
            path=str(out_path),
            media_type="audio/wav",
            filename="warmaster_master.wav",
            headers={"X-Master-Id": master_id}
        )

    return JSONResponse(
        content={
            "ok": True,
            "master_id": master_id,
            "preset": preset,
            "intensity": int(clamp_int(intensity, 0, 100, 55)),
        },
        headers={"X-Master-Id": master_id}
    )


# Opcional: root redirect si no tienes StaticFiles
@app.get("/")
def root():
    if PUBLIC_DIR.exists():
        return RedirectResponse(url="/index.html")
    return {"ok": True, "msg": "WarMaster backend running"}
