from __future__ import annotations

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


# ========================
# CONFIG
# =========================
MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# FREE: procesa solo preview para evitar timeout (render final)
FREE_PREVIEW_SECONDS = 30

# PLUS/PRO: límite por estabilidad
MAX_DURATION_SECONDS_PAID = 6 * 60  # 6 minutos

# Preview real backend (al mover perillas)
PREVIEW_RENDER_SECONDS_DEFAULT = 10
PREVIEW_RENDER_SECONDS_MAX = 15  # para no morir por timeout
PREVIEW_RENDER_TIMEOUT_SECONDS = 90

# Seguridad: si FFmpeg se cuelga, cortamos
FFMPEG_TIMEOUT_SECONDS = 240  # render final

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

app = FastAPI()

# =========================
# STORAGE (MEMORIA)
# =========================
# masters: metadata de masters finales (y también guardaremos source_id si aplica)
masters: Dict[str, Dict[str, Any]] = {}

# sources: guarda el archivo original subido (para previews en tiempo real sin re-subir)
# source_id -> {path, title, created_at, duration, quality}
sources: Dict[str, Dict[str, Any]] = {}

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


def run_cmd(cmd: list[str], timeout_s: int) -> None:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="FFmpeg timeout (proceso muy largo / Render cortó). Prueba con menos segundos."
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


def resolve_source_path(source_id: str) -> Path:
    s = sources.get(source_id)
    if not s:
        raise HTTPException(status_code=404, detail="source_id no encontrado.")
    p = Path(s.get("path", ""))
    if not p.exists():
        raise HTTPException(status_code=404, detail="Archivo source no encontrado en disco.")
    return p


# =========================
# AUDIO FILTER CHAIN
# =========================
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
    Devuelve cadena FFmpeg -af
    - Decimales con punto (.) ✅
    - Sin variables con acento ✅
    - Width con pan compatible ✅
    - Sat sin asoftclip (más compatible) ✅
    """
    intensity_i = clamp_int(intensity, 0, 100, 55)

    thr = -18.0 - (intensity_i * 0.10)
    ratio = 2.0 + (intensity_i * 0.04)

    makeup = 2.0 + (intensity_i * 0.06)
    if makeup != makeup:
        makeup = 2.0
    makeup = max(1.0, min(64.0, makeup))

    preset = (preset or "clean").lower()

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

    eq_live = (
        f"equalizer=f=120:width_type=h:width=1:g={k_low},"
        f"equalizer=f=630:width_type=h:width=1:g={k_mid},"
        f"equalizer=f=1760:width_type=h:width=1:g={k_pres},"
        f"equalizer=f=8500:width_type=h:width=1:g={k_air}"
    )

    glue_p = max(0.0, min(100.0, k_glue)) / 100.0
    glue_thr = -12.0 - glue_p * 18.0
    glue_ratio = 1.2 + glue_p * 3.8
    glue_attack = max(0.001, 0.012 - glue_p * 0.007)
    glue_release = 0.20 + glue_p * 0.10
    glue_comp = (
        f"acompressor=threshold={glue_thr}dB:"
        f"ratio={glue_ratio}:attack={glue_attack}:release={glue_release}:makeup=1"
    )

    k = max(50.0, min(150.0, k_width)) / 100.0
    a = (1.0 + k) / 2.0
    b = (1.0 - k) / 2.0
    width_fx = f"pan=stereo|c0={a:.6f}*c0+{b:.6f}*c1|c1={b:.6f}*c0+{a:.6f}*c1"

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
        f"{width_fx},"
        f"{sat_fx},"
        f"{out_fx},"
        f"{limiter}"
    )


def resolve_download_path(master_id: str) -> Path:
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    out_path = TMP_DIR / f"master_{master_id}.wav"
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    return out_path


# =========================
# ENDPOINTS
# =========================
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me():
    # beta: fijo. luego lo haces real
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


# =========================
# SOURCE UPLOAD (para preview real)
# =========================
@app.post("/api/source")
async def upload_source(
    file: UploadFile = File(...),
    requested_quality: Optional[str] = Form(None),
):
    """
    Sube el audio 1 vez y devuelve source_id.
    Luego /api/preview_render usa ese source_id y re-renderiza previews cortos.
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    source_id = uuid.uuid4().hex[:10]
    name = safe_filename(file.filename)

    src_path = TMP_DIR / f"src_{source_id}_{name}"
    rq = normalize_quality(requested_quality)

    # Guardar archivo
    try:
        with src_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    if not src_path.exists() or src_path.stat().st_size < 1024:
        cleanup_files(src_path)
        raise HTTPException(status_code=400, detail="No se pudo guardar el archivo.")

    # Validar tamaño
    if src_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        cleanup_files(src_path)
        raise HTTPException(status_code=400, detail="Supera 100MB.")

    # Duración y límites
    duration = get_audio_duration_seconds(src_path)
    if rq in ("PLUS", "PRO") and duration > MAX_DURATION_SECONDS_PAID:
        cleanup_files(src_path)
        raise HTTPException(status_code=400, detail="Supera 6 minutos (límite por estabilidad).")

    sources[source_id] = {
        "id": source_id,
        "title": name,
        "path": str(src_path),
        "duration": duration,
        "quality": rq,
        "created_at": datetime.utcnow().isoformat(),
    }

    return JSONResponse({
        "ok": True,
        "source_id": source_id,
        "title": name,
        "duration": duration,
        "quality": rq
    })


# =========================
# PREVIEW REAL BACKEND (FFmpeg) — knobs live
# =========================
@app.post("/api/preview_render")
async def preview_render(
    source_id: str = Form(...),
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

    seconds: int = Form(PREVIEW_RENDER_SECONDS_DEFAULT),
):
    """
    Renderiza un preview CORTO (default 10s) desde el source ya subido.
    Devuelve WAV.
    """
    src_path = resolve_source_path(source_id)

    seconds_i = clamp_int(seconds, 3, PREVIEW_RENDER_SECONDS_MAX, PREVIEW_RENDER_SECONDS_DEFAULT)

    # Clamp knobs
    k_low = clamp_float(k_low, -12.0, 12.0, 0.0)
    k_mid = clamp_float(k_mid, -12.0, 12.0, 0.0)
    k_pres = clamp_float(k_pres, -12.0, 12.0, 0.0)
    k_air = clamp_float(k_air, -12.0, 12.0, 0.0)
    k_glue = clamp_float(k_glue, 0.0, 100.0, 0.0)
    k_width = clamp_float(k_width, 50.0, 150.0, 100.0)
    k_sat = clamp_float(k_sat, 0.0, 100.0, 0.0)
    k_out = clamp_float(k_out, -12.0, 6.0, 0.0)

    filters = preset_chain(
        preset=preset,
        intensity=intensity,
        k_low=k_low, k_mid=k_mid, k_pres=k_pres, k_air=k_air,
        k_glue=k_glue, k_width=k_width, k_sat=k_sat, k_out=k_out
    )

    preview_id = uuid.uuid4().hex[:10]
    out_path = TMP_DIR / f"preview_{source_id}_{preview_id}_{seconds_i}s.wav"

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(src_path),
        "-t", str(int(seconds_i)),
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]

    run_cmd(cmd, timeout_s=PREVIEW_RENDER_TIMEOUT_SECONDS)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(out_path)
        raise HTTPException(status_code=500, detail="Preview vacío.")

    # Nota: no borramos el preview inmediatamente porque lo estamos sirviendo.
    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename=f"warmaster_preview_{seconds_i}s.wav",
        headers={"X-Preview-Id": preview_id, "X-Source-Id": source_id}
    )


# =========================
# MASTER FINAL (Render completo)
# =========================
@app.post("/api/master")
async def master(
    # puedes subir archivo directo como antes...
    file: Optional[UploadFile] = File(None),

    # ...o mandar source_id y no re-subir
    source_id: Optional[str] = Form(None),

    preset: str = Form("clean"),
    intensity: int = Form(55),

    k_low: float = Form(0.0),
    k_mid: float = Form(0.0),
    k_pres: float = Form(0.0),
    k_air: float = Form(0.0),
    k_glue: float = Form(0.0),
    k_width: float = Form(100.0),
    k_sat: float = Form(0.0),
    k_out: float = Form(0.0),

    requested_quality: Optional[str] = Form(None),
    target: Optional[str] = Form(None),
):
    rq = normalize_quality(requested_quality)

    # Determinar input
    use_path: Optional[Path] = None
    original_name = "audio"

    if source_id:
        use_path = resolve_source_path(source_id)
        s = sources.get(source_id) or {}
        original_name = s.get("title") or "audio"
    else:
        if not file or not file.filename:
            raise HTTPException(status_code=400, detail="Archivo inválido (falta file o source_id).")
        original_name = safe_filename(file.filename)

        in_id = uuid.uuid4().hex[:8]
        use_path = TMP_DIR / f"in_{in_id}_{original_name}"

        try:
            with use_path.open("wb") as f:
                shutil.copyfileobj(file.file, f)
        finally:
            try:
                file.file.close()
            except Exception:
                pass

        if use_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            cleanup_files(use_path)
            raise HTTPException(status_code=400, detail="Supera 100MB.")

    # Duración:
    duration = get_audio_duration_seconds(use_path)
    if rq in ("PLUS", "PRO") and duration > MAX_DURATION_SECONDS_PAID:
        if not source_id:
            cleanup_files(use_path)
        raise HTTPException(status_code=400, detail="Supera 6 minutos (límite por estabilidad).")

    # Clamp knobs
    k_low = clamp_float(k_low, -12.0, 12.0, 0.0)
    k_mid = clamp_float(k_mid, -12.0, 12.0, 0.0)
    k_pres = clamp_float(k_pres, -12.0, 12.0, 0.0)
    k_air = clamp_float(k_air, -12.0, 12.0, 0.0)
    k_glue = clamp_float(k_glue, 0.0, 100.0, 0.0)
    k_width = clamp_float(k_width, 50.0, 150.0, 100.0)
    k_sat = clamp_float(k_sat, 0.0, 100.0, 0.0)
    k_out = clamp_float(k_out, -12.0, 6.0, 0.0)

    filters = preset_chain(
        preset=preset,
        intensity=intensity,
        k_low=k_low, k_mid=k_mid, k_pres=k_pres, k_air=k_air,
        k_glue=k_glue, k_width=k_width, k_sat=k_sat, k_out=k_out
    )

    master_id = uuid.uuid4().hex[:8]
    out_path = TMP_DIR / f"master_{master_id}.wav"

    masters[master_id] = {
        "id": master_id,
        "title": original_name,
        "preset": preset,
        "intensity": int(clamp_int(intensity, 0, 100, 55)),
        "quality": rq,
        "created_at": datetime.utcnow().isoformat(),
        "source_id": source_id,
        "knobs": {
            "low": k_low, "mid": k_mid, "pres": k_pres, "air": k_air,
            "glue": k_glue, "width": k_width, "sat": k_sat, "out": k_out
        }
    }

    # FREE: recorta a 30s
    clip_seconds: Optional[int] = FREE_PREVIEW_SECONDS if rq == "FREE" else None

    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(use_path)]
    if clip_seconds:
        cmd += ["-t", str(int(clip_seconds))]

    cmd += [
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]

    try:
        run_cmd(cmd, timeout_s=FFMPEG_TIMEOUT_SECONDS)
    except Exception:
        if not source_id:
            cleanup_files(use_path)
        cleanup_files(out_path)
        raise

    if not out_path.exists() or out_path.stat().st_size < 1024:
        if not source_id:
            cleanup_files(use_path)
        cleanup_files(out_path)
        raise HTTPException(status_code=500, detail="Master vacío.")

    # si el input venía por upload directo, lo borramos
    if not source_id:
        cleanup_files(use_path)

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers={"X-Master-Id": master_id}
    )


# =========================
# STREAM/DOWNLOAD (compat)
# =========================
@app.get("/api/masters/{master_id}/stream")
def api_stream_master(master_id: str):
    return stream_master(master_id)


@app.get("/api/masters/{master_id}/download")
def api_download_master(master_id: str):
    return download_master(master_id)


@app.get("/stream/{master_id}")
def stream_master(master_id: str):
    if master_id not in masters:
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

    return FileResponse(
        path=str(dl_path),
        media_type="audio/wav",
        filename=fname
    )


# =========================
# ROOT + STATIC
# =========================
@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
