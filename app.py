import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

app = FastAPI()

# CORS (si luego separas frontend/otro dominio)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        # devolvemos lo último del stderr para no explotar el tamaño del error
        raise HTTPException(status_code=500, detail=f"FFmpeg error:\n{proc.stderr[-8000:]}")


def preset_chain(preset: str, intensity: int) -> str:
    intensity = max(0, min(100, int(intensity)))

    thr = -18.0 - (intensity * 0.10)
    ratio = 2.0 + (intensity * 0.*
