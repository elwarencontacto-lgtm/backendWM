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

# FREE: procesa solo preview para evitar timeout
FREE_PREVIEW_SECONDS = 30

# PLUS/PRO: límite por estabilidad
MAX_DURATION_SECONDS_PAID = 6 * 60  # 6 minutos

# Seguridad: si FFmpeg se cuelga, cortamos
FFMPEG_TIMEOUT_SECONDS = 240  # 4 min (ajústalo si quieres)

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


def run_cmd(cmd: list[str], timeout_s: int = FFMPEG_TIMEOUT_SECONDS) -> None:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="FFmpeg timeout (proceso muy largo / Render cortó).")

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


def ensure_clean_wav(in_path: Path, clean_path: Path) -> None:
    """
    Convierte el input a WAV PCM 44.1k stereo para:
    - tener una base estable
    - permitir "apply knobs" después sin perder el original
    """
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(in_path),
        "-vn",
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(clean_path)
    ]
    run_cmd(cmd)


def render_master_from_clean(
    clean_path: Path,
    out_path: Path,
    preset: str,
    intensity: int,
    k_low: float, k_mid: float, k_pres: float, k_air: float,
    k_glue: float, k_width: float, k_sat: float, k_out: float,
    clip_seconds: Optional[int] = None,
) -> None:
    filters = preset_chain(
        preset=preset,
        intensity=intensity,
        k_low=k_low, k_mid=k_mid, k_pres=k_pres, k_air=k_air,
        k_glue=k_glue, k_width=k_width, k_sat=k_sat, k_out=k_out
    )

    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(clean_path)]
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
    run_cmd(cmd)


def build_preview_wav(master_id: str, seconds: int) -> Path:
    out_path = TMP_DIR / f"master_{master_id}.wav"
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    seconds = int(max(5, min(60, seconds)))
    prev_path = TMP_DIR / f"preview_{master_id}_{seconds}.wav"

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(out_path),
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
    if master_id not in masters:
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
    # beta: fijo. luego lo haces real (auth/pagos)
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


@app.get("/api/masters/{master_id}/stream")
def api_stream_master(master_id: str):
    return stream_master(master_id)


@app.get("/api/masters/{master_id}/download")
def api_download_master(master_id: str):
    return download_master(master_id)


# LEGACY
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


# LEGACY
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


@app.get("/api/master/preview")
def preview_master(
    master_id: str = Query(...),
    seconds: int = Query(30, ge=5, le=60),
):
    if master_id not in masters:
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
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    master_id = uuid.uuid4().hex[:8]
    name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{master_id}_{name}"
    clean_path = TMP_DIR / f"clean_{master_id}.wav"
    out_path = TMP_DIR / f"master_{master_id}.wav"

    rq = normalize_quality(requested_quality)

    # Guardar metadata
    masters[master_id] = {
        "id": master_id,
        "title": name,
        "preset": preset,
        "intensity": int(clamp_int(intensity, 0, 100, 55)),
        "quality": rq,
        "created_at": datetime.utcnow().isoformat(),
        "paths": {
            "clean": str(clean_path),
            "master": str(out_path),
        },
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

    # Validar duración (solo PAID)
    duration = get_audio_duration_seconds(in_path)
    if rq in ("PLUS", "PRO") and duration > MAX_DURATION_SECONDS_PAID:
        cleanup_files(in_path)
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

    clip_seconds: Optional[int] = FREE_PREVIEW_SECONDS if rq == "FREE" else None

    try:
        # 1) convertir a WAV limpio (se queda guardado para aplicar knobs después)
        ensure_clean_wav(in_path, clean_path)

        # 2) render master desde clean
        render_master_from_clean(
            clean_path=clean_path,
            out_path=out_path,
            preset=preset,
            intensity=intensity,
            k_low=k_low, k_mid=k_mid, k_pres=k_pres, k_air=k_air,
            k_glue=k_glue, k_width=k_width, k_sat=k_sat, k_out=k_out,
            clip_seconds=clip_seconds,
        )
    except Exception:
        cleanup_files(in_path, clean_path, out_path)
        masters.pop(master_id, None)
        raise
    finally:
        cleanup_files(in_path)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(clean_path, out_path)
        masters.pop(master_id, None)
        raise HTTPException(status_code=500, detail="Master vacío.")

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers={"X-Master-Id": master_id}
    )


# ✅ NUEVO: aplicar perillas SOBRE el master ya procesado (re-render desde clean_{id}.wav)
@app.post("/api/master/apply")
async def master_apply(
    master_id: str = Form(...),

    preset: Optional[str] = Form(None),
    intensity: Optional[int] = Form(None),

    k_low: float = Form(0.0),
    k_mid: float = Form(0.0),
    k_pres: float = Form(0.0),
    k_air: float = Form(0.0),
    k_glue: float = Form(0.0),
    k_width: float = Form(100.0),
    k_sat: float = Form(0.0),
    k_out: float = Form(0.0),
):
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    clean_path = Path(m.get("paths", {}).get("clean", ""))
    out_path = TMP_DIR / f"master_{master_id}.wav"
    if not clean_path.exists():
        raise HTTPException(status_code=404, detail="Base (clean) no encontrada. Reprocesa desde cero.")

    # usar preset/intensity guardados si no vienen
    preset_use = (preset if preset is not None else m.get("preset", "clean"))
    intensity_use = int(intensity if intensity is not None else m.get("intensity", 55))

    rq = normalize_quality(m.get("quality"))

    # clamp knobs
    k_low = clamp_float(k_low, -12.0, 12.0, 0.0)
    k_mid = clamp_float(k_mid, -12.0, 12.0, 0.0)
    k_pres = clamp_float(k_pres, -12.0, 12.0, 0.0)
    k_air = clamp_float(k_air, -12.0, 12.0, 0.0)
    k_glue = clamp_float(k_glue, 0.0, 100.0, 0.0)
    k_width = clamp_float(k_width, 50.0, 150.0, 100.0)
    k_sat = clamp_float(k_sat, 0.0, 100.0, 0.0)
    k_out = clamp_float(k_out, -12.0, 6.0, 0.0)

    clip_seconds: Optional[int] = FREE_PREVIEW_SECONDS if rq == "FREE" else None

    # render nuevo master (overwrite)
    render_master_from_clean(
        clean_path=clean_path,
        out_path=out_path,
        preset=preset_use,
        intensity=intensity_use,
        k_low=k_low, k_mid=k_mid, k_pres=k_pres, k_air=k_air,
        k_glue=k_glue, k_width=k_width, k_sat=k_sat, k_out=k_out,
        clip_seconds=clip_seconds,
    )

    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise HTTPException(status_code=500, detail="Master vacío (apply).")

    # actualizar metadata
    m["preset"] = preset_use
    m["intensity"] = intensity_use
    m["knobs"] = {
        "low": k_low, "mid": k_mid, "pres": k_pres, "air": k_air,
        "glue": k_glue, "width": k_width, "sat": k_sat, "out": k_out
    }

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers={"X-Master-Id": master_id}
    )


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


# =========================
# STATIC FILES
# =========================
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")

