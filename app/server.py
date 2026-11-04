"""
Offline Document Translator - Server
=====================================
FastAPI backend with:
- Format-preserving DOCX translation (styles, bold, italic, fonts, tables)
- Document processing pipeline (DOCX, PDF, TXT)
- Smart chunking with paragraph preservation
- Parallel Ollama LLM translation with retry
- Configurable concurrency and num_ctx
- Server-Sent Events for real-time status
- System monitoring (CPU, RAM, GPU via Ollama)
- Connected clients tracking via WebSocket
- Job queue and history
"""

import asyncio
import copy
import json
import logging
import os
import re
import time
import uuid
import traceback
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

import aiohttp
import psutil
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse
try:
    from .security import SharedPasswordMiddleware, configured_password, approved_engine_url, bounded_filename
except ImportError:  # python server.py inside the container
    from security import SharedPasswordMiddleware, configured_password, approved_engine_url, bounded_filename

import docx
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import pdfplumber
import shutil
import subprocess
import tempfile

# PDF conversion
try:
    from pdf2docx import Converter as Pdf2DocxConverter
    PDF2DOCX_AVAILABLE = True
except ImportError:
    PDF2DOCX_AVAILABLE = False

# OCR support
try:
    from PIL import Image
    import pytesseract
    TESSERACT_AVAILABLE = shutil.which('tesseract') is not None
except ImportError:
    TESSERACT_AVAILABLE = False

# ============================================================
# Configuration
# ============================================================

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# OLLAMA_URL can be 'auto' (set by entrypoint.sh after detection) or explicit URL
_ollama_env = os.environ.get("OLLAMA_URL", "http://host.containers.internal:11434")
DEFAULT_OLLAMA_URL = _ollama_env if _ollama_env != "auto" else "http://host.containers.internal:11434"
DEFAULT_LIBRETRANSLATE_URL = os.environ.get("LIBRETRANSLATE_URL", "http://127.0.0.1:5000")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "translategemma")
DEFAULT_ENGINE = os.environ.get("TRANSLATE_ENGINE", "ollama")  # 'ollama' or 'libretranslate'
MAX_CHUNK_CHARS = 3000
DEFAULT_BATCH_SIZE = 3          # segments per Ollama request
MAX_RETRIES = 3
RETRY_DELAY = 2
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
DEFAULT_CONCURRENCY = 3
DEFAULT_NUM_CTX = 4096
DEFAULT_KEEP_ALIVE = "30m"


def validate_engine_url(url: str, kind: str = "ollama") -> str:
    default = DEFAULT_LIBRETRANSLATE_URL if kind == "libretranslate" else DEFAULT_OLLAMA_URL
    try:
        return approved_engine_url(url or default, [DEFAULT_OLLAMA_URL, DEFAULT_LIBRETRANSLATE_URL])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def upload_filename(filename: str) -> str:
    try:
        return bounded_filename(filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def validate_languages(source: str, target: str):
    if source not in LANG_EN and source != "auto":
        raise HTTPException(400, "Limba sursă este invalidă.")
    if target not in LANG_EN:
        raise HTTPException(400, "Limba țintă este invalidă.")

LANG_EN = {
    'ro': 'Romanian', 'en': 'English', 'fr': 'French', 'de': 'German',
    'es': 'Spanish', 'it': 'Italian', 'pt': 'Portuguese', 'pb': 'Portuguese (Brazil)',
    'nl': 'Dutch', 'pl': 'Polish', 'cs': 'Czech', 'sk': 'Slovak', 'hu': 'Hungarian',
    'bg': 'Bulgarian', 'sl': 'Slovenian', 'sq': 'Albanian',
    'uk': 'Ukrainian', 'ru': 'Russian', 'el': 'Greek', 'tr': 'Turkish',
    'ar': 'Arabic', 'he': 'Hebrew', 'fa': 'Persian', 'hi': 'Hindi',
    'bn': 'Bengali', 'ur': 'Urdu', 'ja': 'Japanese', 'ko': 'Korean',
    'zh': 'Chinese', 'zt': 'Chinese Traditional',
    'th': 'Thai', 'vi': 'Vietnamese', 'id': 'Indonesian', 'ms': 'Malay',
    'sv': 'Swedish', 'da': 'Danish', 'nb': 'Norwegian', 'fi': 'Finnish',
    'et': 'Estonian', 'lt': 'Lithuanian', 'lv': 'Latvian',
    'ca': 'Catalan', 'gl': 'Galician', 'eu': 'Basque',
    'ga': 'Irish', 'tl': 'Tagalog', 'az': 'Azerbaijani', 'ky': 'Kyrgyz',
    'eo': 'Esperanto'
}

def _get_installed_languages():
    """Read installed language codes from build-time file or env var."""
    try:
        lang_file = Path("/app/installed_languages.txt")
        if lang_file.exists():
            return lang_file.read_text().strip().split(",")
    except:
        pass
    env_langs = os.environ.get("INSTALLED_LANGUAGES", "")
    if env_langs:
        return [l.strip() for l in env_langs.split(",") if l.strip()]
    return ["en", "ro"]

# ============================================================
# In-Memory Log Handler
# ============================================================

class InMemoryLogHandler(logging.Handler):
    def __init__(self, max_entries=2000):
        super().__init__()
        self.logs = deque(maxlen=max_entries)

    def emit(self, record):
        entry = {
            "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "source": record.name,
            "message": self.format(record)
        }
        self.logs.append(entry)

log_handler = InMemoryLogHandler(max_entries=2000)
log_handler.setFormatter(logging.Formatter('%(name)s - %(message)s'))
log_handler.setLevel(logging.DEBUG)

# Attach to root logger and uvicorn loggers
logging.getLogger().addHandler(log_handler)
logging.getLogger().setLevel(logging.INFO)
for _ln in ['uvicorn', 'uvicorn.access', 'uvicorn.error', 'fastapi']:
    logging.getLogger(_ln).addHandler(log_handler)

app_logger = logging.getLogger('translator')
app_logger.setLevel(logging.DEBUG)

# ============================================================
# App Setup
# ============================================================

app = FastAPI(title="Offline Document Translator")
app.add_middleware(SharedPasswordMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

@app.get("/logs.html")
async def serve_logs_page():
    logs_path = BASE_DIR / "static" / "logs.html"
    if logs_path.exists():
        return FileResponse(str(logs_path), media_type="text/html")
    raise HTTPException(404, "Logs page not found")

@app.get("/prompts.html")
async def serve_prompts_page():
    page = BASE_DIR / "static" / "prompts.html"
    if page.exists():
        return FileResponse(str(page), media_type="text/html")
    raise HTTPException(404, "Prompts page not found")

# ============================================================
# Global State
# ============================================================

class JobStatus(str, Enum):
    QUEUED = "queued"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    TRANSLATING = "translating"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TranslationJob:
    def __init__(self, job_id, filename, source_lang, target_lang, model, ollama_url,
                 client_ip, concurrency=DEFAULT_CONCURRENCY, num_ctx=DEFAULT_NUM_CTX,
                 engine="ollama", libretranslate_url=DEFAULT_LIBRETRANSLATE_URL,
                 convert_to_pdf=False, batch_size=DEFAULT_BATCH_SIZE,
                 keep_alive=DEFAULT_KEEP_ALIVE, chunk_size=MAX_CHUNK_CHARS,
                 original_prompt=False):
        self.job_id = job_id
        self.filename = filename
        self.display_filename = filename
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.model = model
        self.ollama_url = ollama_url
        self.client_ip = client_ip
        self.concurrency = concurrency
        self.num_ctx = num_ctx
        self.engine = engine  # 'ollama' or 'libretranslate'
        self.libretranslate_url = libretranslate_url
        self.convert_to_pdf = convert_to_pdf
        self.batch_size = batch_size
        self.keep_alive = keep_alive
        self.chunk_size = chunk_size
        self.original_prompt = original_prompt  # use native TranslateGemma prompt
        self.status = JobStatus.QUEUED
        self.progress = 0.0
        self.current_step = "Queued"
        self.kind = "translate"     # 'translate' | 'verify' (external-document review)
        self.total_chunks = 0
        self.completed_chunks = 0
        self.failed_chunks = 0
        self.retried_chunks = 0
        self.total_chars = 0
        self.translated_chars = 0
        self.error = None
        self.created_at = datetime.now().isoformat()
        self.started_at = None
        self.completed_at = None
        self.output_file = None
        self.chunks = []            # for TXT/PDF
        self.translated_chunks = [] # for TXT/PDF
        self.docx_segments = []     # for DOCX format-preserving
        self.source_segments = []   # clean original text per segment (for review)
        self.events = []
        self.cancelled = False
        self.original_filename = None   # name of the original (source-language) file for review
        self.original_docx_segments = [] # DOCX segment metadata for the original file
        # --- LLM review state ---
        self.review_status = None       # None | 'running' | 'completed' | 'failed'
        self.review_model = None
        self.review_progress = 0.0
        self.review_total = 0
        self.review_done = 0
        self.review_results = []        # list of per-segment review dicts
        self.review_summary = None      # {ok, minor, major, errors, avg_score}
        self.review_error = None
        self.review_cancel = False
        self.revised_output_file = None  # revised document after applying suggestions
        self.review_export_file = None    # HTML export of review suggestions

    def add_event(self, etype, message, detail=""):
        self.events.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "type": etype,
            "message": message,
            "detail": detail
        })

    def to_dict(self):
        elapsed = None
        if self.started_at:
            end = self.completed_at or datetime.now().isoformat()
            try:
                t1 = datetime.fromisoformat(self.started_at)
                t2 = datetime.fromisoformat(end)
                elapsed = round((t2 - t1).total_seconds(), 1)
            except:
                pass
        return {
            "job_id": self.job_id,
            "filename": self.display_filename,
            "kind": self.kind,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "model": self.model,
            "client_ip": self.client_ip,
            "status": self.status.value,
            "progress": round(self.progress, 1),
            "current_step": self.current_step,
            "total_chunks": self.total_chunks,
            "completed_chunks": self.completed_chunks,
            "failed_chunks": self.failed_chunks,
            "retried_chunks": self.retried_chunks,
            "total_chars": self.total_chars,
            "translated_chars": self.translated_chars,
            "concurrency": self.concurrency,
            "num_ctx": self.num_ctx,
            "engine": self.engine,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": elapsed,
            "output_file": self.output_file,
            "batch_size": self.batch_size,
            "keep_alive": self.keep_alive,
            "chunk_size": self.chunk_size,
            "original_prompt": self.original_prompt,
            "review": {
                "status": self.review_status,
                "model": self.review_model,
                "progress": round(self.review_progress, 1),
                "total": self.review_total,
                "done": self.review_done,
                "summary": self.review_summary,
                "error": self.review_error,
                "revised_output_file": self.revised_output_file,
            },
            "events": self.events[-100:]
        }

# Stores
jobs: dict[str, TranslationJob] = {}
sse_queues: dict[str, list[asyncio.Queue]] = {}
connected_clients: dict[str, dict] = {}
server_start_time = datetime.now()


# Own asynchronous work so cancellation closes pending HTTP requests before files
# are removed. Registries are process-local, like the job history itself.
pipeline_tasks: dict[str, asyncio.Task] = {}
review_tasks: dict[str, asyncio.Task] = {}
_active_file_paths: dict[Path, int] = {}
_file_activity_lock = threading.RLock()


def _track_job_task(registry, job_id, coroutine):
    task = asyncio.create_task(coroutine)
    if task is not None:
        registry[job_id] = task
        def finished(done):
            if registry.get(job_id) is done:
                registry.pop(job_id, None)
        task.add_done_callback(finished)
    return task


def start_pipeline(job):
    if getattr(job, "deleting", False):
        raise HTTPException(409, "Jobul este în curs de ștergere.")
    return _track_job_task(pipeline_tasks, job.job_id, run_pipeline(job))


async def _review_task(job, model, url, **kwargs):
    try:
        await run_review(job, model, url, **kwargs)
    except asyncio.CancelledError:
        job.review_cancel = True
        job.review_status = "cancelled"
        job.review_error = None
        job.add_event("warning", "Verificare anulată")
        emit(job)
        raise


def start_review(job, model, url, **kwargs):
    if getattr(job, "deleting", False):
        raise HTTPException(409, "Jobul este în curs de ștergere.")
    return _track_job_task(review_tasks, job.job_id,
                           _review_task(job, model, url, **kwargs))


async def cancel_job_tasks(job, *, translation=True, review=True):
    tasks = []
    if translation:
        job.cancelled = True
        task = pipeline_tasks.get(job.job_id)
        if task is not None and not task.done():
            task.cancel()
            tasks.append(task)
    if review:
        job.review_cancel = True
        task = review_tasks.get(job.job_id)
        if task is not None and not task.done():
            task.cancel()
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # Cancellation can happen before a newly scheduled coroutine executes.
    if translation and job.status in (JobStatus.QUEUED, JobStatus.EXTRACTING,
            JobStatus.CHUNKING, JobStatus.TRANSLATING, JobStatus.ASSEMBLING):
        job.status = JobStatus.CANCELLED
        job.current_step = "Cancelled by user"
        job.completed_at = datetime.now().isoformat()
    if review and job.review_status == "running":
        job.review_status = "cancelled"
    emit(job)


from contextlib import contextmanager


@contextmanager
def file_lease(*paths):
    """Protect staged OCR/document files while a request is using them."""
    resolved = {Path(path).resolve() for path in paths}
    with _file_activity_lock:
        for path in resolved:
            _active_file_paths[path] = _active_file_paths.get(path, 0) + 1
    try:
        yield
    finally:
        with _file_activity_lock:
            for path in resolved:
                remaining = _active_file_paths[path] - 1
                if remaining:
                    _active_file_paths[path] = remaining
                else:
                    _active_file_paths.pop(path)


def is_file_active(path):
    path = Path(path)
    with _file_activity_lock:
        if path.resolve() in _active_file_paths:
            return True
    for job in list(jobs.values()):
        active = (job.status in (JobStatus.QUEUED, JobStatus.EXTRACTING,
                  JobStatus.CHUNKING, JobStatus.TRANSLATING, JobStatus.ASSEMBLING)
                  or job.review_status == "running"
                  or any(task is not None and not task.done() for task in
                         (pipeline_tasks.get(job.job_id), review_tasks.get(job.job_id))))
        if active and path.name.startswith(job.job_id + "_"):
            return True
    return False

# ============================================================
# Prompt / Response history ("CMD" istoric)
# Records the exact request sent to the translation engine and the
# raw response received, for debugging and transparency.
# ============================================================

prompt_log: deque = deque(maxlen=1000)

def record_exchange(job_id, engine, model, src, tgt, kind,
                    prompt, response, attempts, duration, error=None):
    """Store one translation request/response exchange in the history."""
    try:
        prompt_log.append({
            "id": str(uuid.uuid4())[:8],
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "job_id": job_id or "-",
            "engine": engine,
            "model": model or "-",
            "src": src,
            "tgt": tgt,
            "kind": kind,                       # 'single' | 'batch' | 'libretranslate'
            "prompt": prompt or "",
            "response": response or "",
            "prompt_chars": len(prompt or ""),
            "response_chars": len(response or ""),
            "attempts": attempts,
            "duration_ms": round((duration or 0) * 1000),
            "error": error,
        })
    except Exception:
        pass

# ============================================================
# WebSocket for Dashboard
# ============================================================

dashboard_ws_clients: list[WebSocket] = []

async def broadcast_dashboard(data: dict):
    dead = []
    for ws in dashboard_ws_clients:
        try:
            await ws.send_json(data)
        except:
            dead.append(ws)
    for ws in dead:
        if ws in dashboard_ws_clients:
            dashboard_ws_clients.remove(ws)

@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    await websocket.accept()
    dashboard_ws_clients.append(websocket)
    client_ip = websocket.client.host if websocket.client else "unknown"
    ws_id = str(uuid.uuid4())[:8]
    connected_clients[ws_id] = {
        "ip": client_ip,
        "connected_at": datetime.now().isoformat(),
        "page": "dashboard"
    }
    try:
        await websocket.send_json({"type": "init", "data": await get_dashboard_data()})
        while True:
            data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except (WebSocketDisconnect, asyncio.TimeoutError, Exception):
        pass
    finally:
        if websocket in dashboard_ws_clients:
            dashboard_ws_clients.remove(websocket)
        connected_clients.pop(ws_id, None)

# ============================================================
# System Monitoring
# ============================================================

async def get_system_stats():
    cpu_percent = psutil.cpu_percent(interval=0)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    gpu_info = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DEFAULT_OLLAMA_URL}/api/ps",
                                   timeout=aiohttp.ClientTimeout(total=3), allow_redirects=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("models"):
                        gpu_info = [{"model": m.get("name","unknown"),
                                     "size": m.get("size",0),
                                     "vram": m.get("size_vram",0),
                                     "expires_at": m.get("expires_at","")}
                                    for m in data["models"]]
    except:
        pass
    return {
        "cpu_percent": cpu_percent, "cpu_count": psutil.cpu_count(),
        "ram_total_gb": round(mem.total/(1024**3),1), "ram_used_gb": round(mem.used/(1024**3),1),
        "ram_percent": mem.percent,
        "disk_total_gb": round(disk.total/(1024**3),1), "disk_used_gb": round(disk.used/(1024**3),1),
        "disk_percent": round(disk.percent,1), "gpu_models": gpu_info
    }

async def get_ollama_status(url=None):
    target_url = url or DEFAULT_OLLAMA_URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{target_url}/api/tags",
                                   timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = [{"name": m.get("name",""), "size": m.get("size",0),
                               "modified_at": m.get("modified_at",""),
                               "details": m.get("details",{})}
                              for m in data.get("models",[])]
                    return {"connected": True, "models": models}
    except:
        pass
    return {"connected": False, "models": []}

async def get_libretranslate_status(url=None):
    target_url = url or DEFAULT_LIBRETRANSLATE_URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{target_url}/languages",
                                   timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False) as resp:
                if resp.status == 200:
                    langs = await resp.json()
                    if isinstance(langs, list) and langs and all(isinstance(entry, dict) and isinstance(entry.get("code"), str) for entry in langs):
                        return {"connected": True, "languages": len(langs), "url": target_url}
    except:
        pass
    return {"connected": False, "languages": 0, "url": target_url}

def check_libreoffice_available():
    """Check if LibreOffice headless is available."""
    try:
        result = subprocess.run(['libreoffice', '--version'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return {"available": True, "version": result.stdout.strip()}
    except:
        pass
    try:
        result = subprocess.run(['soffice', '--version'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return {"available": True, "version": result.stdout.strip()}
    except:
        pass
    return {"available": False, "version": None}

def convert_docx_to_pdf_libreoffice(docx_path: Path, output_dir: Path) -> Path:
    """Convert DOCX to PDF using LibreOffice headless."""
    try:
        executable = shutil.which('libreoffice') or shutil.which('soffice')
        if not executable:
            raise RuntimeError('LibreOffice/soffice nu este instalat.')
        cmd = [executable, '--headless', '--convert-to', 'pdf',
               '--outdir', str(output_dir), str(docx_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        fallback = shutil.which('soffice')
        if result.returncode != 0 and fallback and fallback != executable:
            cmd[0] = fallback
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        pdf_path = output_dir / (docx_path.stem + '.pdf')
        if result.returncode == 0 and pdf_path.exists():
            return pdf_path
        raise Exception(f"PDF not created: {result.stderr}")
    except subprocess.TimeoutExpired:
        raise Exception("LibreOffice conversion timed out (120s)")

async def get_dashboard_data():
    sys_stats = await get_system_stats()
    ollama = await get_ollama_status()
    libretranslate = await get_libretranslate_status()
    active_jobs = [j.to_dict() for j in jobs.values()
                   if j.status in (JobStatus.QUEUED, JobStatus.EXTRACTING,
                                   JobStatus.CHUNKING, JobStatus.TRANSLATING, JobStatus.ASSEMBLING)]
    completed_jobs = sorted([j.to_dict() for j in jobs.values()
                             if j.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)],
                            key=lambda x: x["created_at"], reverse=True)
    uptime = (datetime.now() - server_start_time).total_seconds()
    return {
        "system": sys_stats, "ollama": ollama, "libretranslate": libretranslate,
        "connected_clients": len(connected_clients),
        "clients_detail": list(connected_clients.values()),
        "active_jobs": active_jobs, "completed_jobs": completed_jobs[:50],
        "total_jobs": len(jobs), "uptime_seconds": round(uptime)
    }

async def dashboard_updater():
    while True:
        await asyncio.sleep(3)
        if dashboard_ws_clients:
            try:
                data = await get_dashboard_data()
                await broadcast_dashboard({"type": "update", "data": data})
            except:
                pass

@app.on_event("startup")
async def startup():
    asyncio.create_task(dashboard_updater())
    schedule_daily_cleanup()

# ============================================================
# DOCX Format-Preserving Extraction
# ============================================================

# Inline markers used to preserve mid-paragraph formatting (bold/italic/color).
# A paragraph with multiple differently-formatted run groups is sent to the
# engine as: ⟦0⟧text-of-group-0⟦1⟧text-of-group-1...
# The engine is instructed to keep the markers; we then redistribute the
# translated text back into the original runs, preserving each run's format.
INLINE_MARKER_RE = re.compile(r"⟦\d+⟧")  # ⟦n⟧


def _paragraph_runs(para):
    """Include hyperlink runs in reading order without flattening their XML."""
    from docx.text.run import Run
    return [Run(element, para) for element in para._p.xpath('./w:r | ./w:hyperlink/w:r')]


def _run_format_signature(run):
    font = run.font
    try:
        color = str(font.color.rgb) if font.color is not None and font.color.type is not None else None
    except Exception:
        color = None
    try:
        size = font.size.pt if font.size is not None else None
    except Exception:
        size = None
    parent = run._r.getparent()
    hyperlink = tuple(sorted(parent.attrib.items())) if parent.tag == qn('w:hyperlink') else None
    return (run.bold, run.italic, run.underline, font.name, size, color, hyperlink)


def _paragraph_format_groups(para) -> list[dict]:
    groups = []
    for ri, run in enumerate(_paragraph_runs(para)):
        if not run.text:
            continue
        sig = _run_format_signature(run)
        if groups and groups[-1]['sig'] == sig:
            groups[-1]['run_indices'].append(ri)
            groups[-1]['text'] += run.text
        else:
            groups.append({'sig': sig, 'run_indices': [ri], 'text': run.text})
    return groups


def _set_run_text(run, text):
    """Replace textual children only; Run.text also deletes drawings/objects."""
    from docx.text.run import Run
    textual = {qn('w:t'), qn('w:tab'), qn('w:br'), qn('w:cr')}
    def is_text(child):
        return child.tag in textual and not (child.tag == qn('w:br') and child.get(qn('w:type')) in ('page', 'column'))
    positions = [i for i, child in enumerate(run._r) if is_text(child)]
    insert_at = positions[0] if positions else len(run._r)
    for child in list(run._r):
        if is_text(child):
            run._r.remove(child)
    scratch = Run(OxmlElement('w:r'), run._parent)
    scratch.text = text
    for offset, child in enumerate(list(scratch._r)):
        run._r.insert(insert_at + offset, child)


def extract_docx_segments(filepath: Path, tag_inline: bool = True) -> list[dict]:
    """Record exact locations for body, tables, headers and footers.

    A merged cell or linked header/footer is visited once. Hyperlink text and
    each cell paragraph retain their own formatting boundaries.
    """
    document = docx.Document(str(filepath))
    segments = []

    def add_paragraph(para, location):
        groups = _paragraph_format_groups(para)
        full_text = ''.join(group['text'] for group in groups).strip()
        if not full_text:
            return
        multi_run = tag_inline and len(groups) > 1
        text = (''.join(f"⟦{i}⟧{group['text']}" for i, group in enumerate(groups))
                if multi_run else full_text)
        segments.append({'index': len(segments), 'text': text, 'char_count': len(full_text),
                         'multi_run': multi_run, **location})

    def visit_table(table, table_index, table_path, story):
        seen_cells = set()
        for ri, row in enumerate(table.rows):
            for ci, cell in enumerate(row.cells):
                if cell._tc in seen_cells:
                    continue
                seen_cells.add(cell._tc)
                for pi, para in enumerate(cell.paragraphs):
                    add_paragraph(para, {'type': 'table_cell', 'table_index': table_index,
                                        'table_path': table_path, 'row_index': ri,
                                        'cell_index': ci, 'cell_para_index': pi, **story})
                for ni, nested in enumerate(cell.tables):
                    visit_table(nested, table_index, table_path + [(ri, ci, ni)], story)

    def visit_story(container, story):
        for pi, para in enumerate(container.paragraphs):
            add_paragraph(para, {'type': 'paragraph', 'para_index': pi, **story})
        for ti, table in enumerate(container.tables):
            visit_table(table, ti, [], story)

    visit_story(document, {})
    seen_stories = set()
    for si, section in enumerate(document.sections):
        for kind in ('header', 'first_page_header', 'even_page_header',
                     'footer', 'first_page_footer', 'even_page_footer'):
            container = getattr(section, kind)
            # Do not create absent parts just by inspecting the document.
            if not container._has_definition:
                continue
            if container.part.partname in seen_stories:
                continue
            seen_stories.add(container.part.partname)
            visit_story(container, {'story': kind, 'section_index': si})
    return segments


def align_review_segments(source_texts, source_segments, target_texts, target_segments):
    """Align each DOCX story separately; unmatched target content stays intact."""
    if source_segments is None or target_segments is None:
        n = min(len(source_texts), len(target_texts))
        return source_texts[:n], n, len(source_texts) != len(target_texts)
    source_groups = defaultdict(list)
    target_groups = defaultdict(list)
    for seg in source_segments:
        source_groups[(seg.get('story', 'body'), seg.get('section_index', 0))].append(seg)
    for seg in target_segments:
        target_groups[(seg.get('story', 'body'), seg.get('section_index', 0))].append(seg)
    aligned = [''] * len(target_texts)
    paired = 0
    mismatch = False
    for key in source_groups.keys() | target_groups.keys():
        src = source_groups[key]
        tgt = target_groups[key]
        mismatch |= len(src) != len(tgt)
        for original, translated in zip(src, tgt):
            aligned[translated['index']] = source_texts[original['index']]
            paired += 1
    return aligned, paired, mismatch


def apply_translations_to_docx(filepath: Path, segments: list[dict],
                               translations: list[str], output_path: Path,
                               allow_empty: bool = False):
    document = docx.Document(str(filepath))
    for seg, trans in zip(segments, translations):
        if trans is None or (not trans and not allow_empty) or trans.startswith('[TRANSLATION ERROR'):
            continue
        container = (getattr(document.sections[seg['section_index']], seg['story'])
                     if seg.get('story') else document)
        if seg['type'] == 'paragraph':
            para = container.paragraphs[seg['para_index']]
        elif seg['type'] == 'table_cell':
            table = container.tables[seg['table_index']]
            for ri, ci, ni in seg.get('table_path', []):
                table = table.rows[ri].cells[ci].tables[ni]
            para = table.rows[seg['row_index']].cells[seg['cell_index']].paragraphs[seg.get('cell_para_index', 0)]
        else:
            continue
        _apply_paragraph_translation(para, trans, seg.get('multi_run', False))
    document.save(str(output_path))


def _apply_paragraph_translation(para, new_text: str, multi_run: bool):
    runs = _paragraph_runs(para)
    groups = _paragraph_format_groups(para)
    matches = list(INLINE_MARKER_RE.finditer(new_text)) if multi_run else []
    marker_ids = [int(match.group()[1:-1]) for match in matches]
    if (groups and matches and len(matches) == len(groups)
            and set(marker_ids) == set(range(len(groups)))
            and not new_text[:matches[0].start()].strip()):
        pieces = [new_text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(new_text)]
                  for i, m in enumerate(matches)]
        if marker_ids == list(range(len(groups))):
            for group, piece in zip(groups, pieces):
                _set_run_text(runs[group['run_indices'][0]], piece)
                for ri in group['run_indices'][1:]:
                    _set_run_text(runs[ri], '')
            return
        # The translated word order can differ. Rebuild textual carriers in that
        # order using the styles/link target identified by each marker; retain
        # non-text XML (drawings, fields, objects) in the original paragraph.
        from docx.text.run import Run
        first = runs[groups[0]['run_indices'][0]]._r
        while first.getparent() is not para._p:
            first = first.getparent()
        insertion = para._p.index(first)
        carriers = []
        for marker_id, piece in zip(marker_ids, pieces):
            source = runs[groups[marker_id]['run_indices'][0]]
            element = OxmlElement('w:r')
            if source._r.rPr is not None:
                element.append(copy.deepcopy(source._r.rPr))
            Run(element, para).text = piece
            parent = source._r.getparent()
            if parent.tag == qn('w:hyperlink'):
                link = OxmlElement('w:hyperlink')
                for key, value in parent.attrib.items():
                    link.set(key, value)
                link.append(element)
                element = link
            carriers.append(element)
        for run in runs:
            _set_run_text(run, '')
        for offset, element in enumerate(carriers):
            para._p.insert(insertion + offset, element)
        return

    clean = INLINE_MARKER_RE.sub('', new_text)
    if groups:
        first = groups[0]['run_indices'][0]
        for ri, run in enumerate(runs):
            _set_run_text(run, clean if ri == first else '')
    elif runs:
        _set_run_text(runs[0], clean)
    else:
        para.add_run(clean)


def _replace_paragraph_text(para, new_text: str):
    _apply_paragraph_translation(para, new_text, False)


def _clear_paragraph(para):
    for run in _paragraph_runs(para):
        _set_run_text(run, '')


def _page_requires_ocr(page):
    text = (page.extract_text() or '').strip()
    # A page number or a short digital heading does not turn a scanned page into
    # a fully digital page. Inspect every page, including pages after the fifth.
    image_area = sum(max(0, image['x1'] - image['x0']) * max(0, image['bottom'] - image['top'])
                     for image in page.images)
    return not text or (image_area > page.width * page.height * 0.5 and len(text) < 100)


def pdf_has_selectable_text(filepath: Path) -> bool:
    """True only if every nonblank page can be handled without OCR."""
    with pdfplumber.open(str(filepath)) as pdf:
        return bool(pdf.pages) and all(not _page_requires_ocr(page) for page in pdf.pages)


def _ocr_pdf_page(pdf_path: Path, page_number: int, lang: str):
    if not TESSERACT_AVAILABLE:
        raise RuntimeError('Tesseract OCR nu este disponibil pentru paginile scanate.')
    from pdf2image import convert_from_path
    images = convert_from_path(str(pdf_path), dpi=300, first_page=page_number,
                               last_page=page_number, thread_count=1)
    try:
        return pytesseract.image_to_string(images[0], lang=lang).strip()
    finally:
        for image in images:
            image.close()


def convert_pdf_to_docx(pdf_path: Path, docx_path: Path):
    if not PDF2DOCX_AVAILABLE:
        raise RuntimeError('pdf2docx nu este instalat.')
    converter = Pdf2DocxConverter(str(pdf_path))
    try:
        converter.convert(str(docx_path), multi_processing=False)
    finally:
        converter.close()


def prepare_pdf_for_translation(pdf_path: Path, docx_path: Path, lang: str = 'eng+ron+fra'):
    """Convert digital pages and OCR scanned pages, preserving their order.

    Digital pages keep pdf2docx layout/media. Scanned pages become editable OCR
    paragraphs, rather than leaving an untranslated full-page image in output.
    """
    with pdfplumber.open(str(pdf_path)) as pdf:
        scanned = [_page_requires_ocr(page) for page in pdf.pages]
        dimensions = [(page.width, page.height) for page in pdf.pages]
        image_pages = [bool(page.images) for page in pdf.pages]
    if not scanned:
        raise ValueError('PDF-ul nu conține pagini.')
    if not any(scanned):
        convert_pdf_to_docx(pdf_path, docx_path)
        return
    from docxcompose.composer import Composer
    from docx.shared import Pt
    with tempfile.TemporaryDirectory(prefix='translator-pdf-') as work:
        composer = None
        for index, needs_ocr in enumerate(scanned):
            page_path = Path(work) / f'page-{index}.docx'
            if needs_ocr:
                page_doc = docx.Document()
                page_doc.sections[0].page_width = Pt(dimensions[index][0])
                page_doc.sections[0].page_height = Pt(dimensions[index][1])
                text = _ocr_pdf_page(pdf_path, index + 1, lang)
                if not text.strip() and image_pages[index]:
                    raise ValueError(f'Nu s-a putut extrage text de pe pagina scanată {index + 1}. Verifică limba OCR și lizibilitatea scanării.')
                for paragraph in re.split(r'\n\s*\n', text):
                    if paragraph.strip():
                        page_doc.add_paragraph(paragraph.strip())
            else:
                if not PDF2DOCX_AVAILABLE:
                    raise RuntimeError('pdf2docx nu este instalat pentru paginile digitale.')
                converter = Pdf2DocxConverter(str(pdf_path))
                try:
                    converter.convert(str(page_path), start=index, end=index + 1, multi_processing=False)
                finally:
                    converter.close()
                page_doc = docx.Document(str(page_path))
            if composer is None:
                composer = Composer(page_doc)
            else:
                composer.doc.add_page_break()
                composer.append(page_doc)
        composer.save(str(docx_path))


def ocr_pdf_to_docx(pdf_path: Path, docx_path: Path, lang: str = 'eng+ron+fra'):
    prepare_pdf_for_translation(pdf_path, docx_path, lang)


def extract_text_from_pdf(filepath: Path) -> list[str]:
    paragraphs = []
    with pdfplumber.open(str(filepath)) as pdf:
        for index, page in enumerate(pdf.pages):
            text = (_ocr_pdf_page(filepath, index + 1, 'eng+ron+fra')
                    if _page_requires_ocr(page) else page.extract_text() or '')
            paragraphs.extend(p.strip().replace('\n', ' ') for p in re.split(r'\n\s*\n', text) if p.strip())
    return paragraphs


def extract_text_from_txt(filepath: Path) -> list[str]:
    text = filepath.read_text(encoding='utf-8', errors='replace')
    return [paragraph.strip() for paragraph in re.split(r'\n\s*\n', text) if paragraph.strip()]


def extract_review_segments(filepath: Path):
    ext = filepath.suffix.lower()
    if ext == '.pdf':
        converted = Path(str(filepath) + '.review.docx')
        prepare_pdf_for_translation(filepath, converted)
        segments = extract_docx_segments(converted, tag_inline=True)
        return [seg['text'] for seg in segments], segments
    if ext == '.docx':
        segments = extract_docx_segments(filepath, tag_inline=True)
        return [seg['text'] for seg in segments], segments
    if ext in ('.txt', '.text', '.md'):
        return extract_text_from_txt(filepath), None
    raise ValueError(f'Format nesuportat: {ext}')

# ============================================================
# Smart Chunking (for plain text pipeline)
# ============================================================

def split_text_bounded(text: str, max_chars: int) -> list[str]:
    """Split without dropping text, preferring word boundaries and whole markers."""
    if max_chars < 1:
        raise ValueError("Chunk size must be positive")
    parts = []
    while len(text) > max_chars:
        cut = max_chars
        # A marker must remain one token so DOCX formatting can be rebuilt.
        for marker in INLINE_MARKER_RE.finditer(text):
            if marker.start() < cut < marker.end():
                cut = marker.start()
                break
            if marker.start() >= cut:
                break
        if cut == 0:
            raise ValueError("Chunk size is too small for an inline marker")
        whitespace = list(re.finditer(r"\s+", text[:cut]))
        if whitespace and whitespace[-1].end() >= max_chars // 2:
            cut = whitespace[-1].end()
        parts.append(text[:cut])
        text = text[cut:]
    if text:
        parts.append(text)
    return parts


def create_chunks(paragraphs: list[str], max_chars: int = MAX_CHUNK_CHARS) -> list[dict]:
    chunks = []
    current = ""
    for para in paragraphs:
        if not para:
            continue
        if len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(split_text_bounded(para, max_chars))
        elif current and len(current) + 2 + len(para) > max_chars:
            chunks.append(current)
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(current)
    return [{"index": i, "text": text, "char_count": len(text)}
            for i, text in enumerate(chunks)]

# ============================================================
# LibreTranslate Language Code Mapping
# ============================================================

# Map our lang codes to LibreTranslate codes
LIBRETRANSLATE_LANG_MAP = {
    'ro': 'ro', 'en': 'en', 'fr': 'fr', 'de': 'de', 'es': 'es', 'it': 'it',
    'pt': 'pt', 'nl': 'nl', 'pl': 'pl', 'cs': 'cs', 'sk': 'sk', 'hu': 'hu',
    'bg': 'bg', 'hr': 'hr', 'sl': 'sl', 'sr': 'sr', 'sq': 'sq', 'mk': 'mk',
    'uk': 'uk', 'ru': 'ru', 'el': 'el', 'tr': 'tr', 'ar': 'ar', 'he': 'he',
    'fa': 'fa', 'hi': 'hi', 'bn': 'bn', 'ja': 'ja', 'ko': 'ko',
    'zh-Hans': 'zh', 'zh-Hant': 'zt', 'th': 'th', 'vi': 'vi',
    'id': 'id', 'ms': 'ms', 'sv': 'sv', 'da': 'da', 'no': 'nb',
    'fi': 'fi', 'et': 'et', 'lt': 'lt', 'lv': 'lv', 'af': 'af',
    'sw': 'sw', 'ca': 'ca', 'gl': 'gl', 'cy': 'cy', 'ga': 'ga',
    'mt': 'mt', 'ta': 'ta', 'te': 'te', 'kn': 'kn', 'ml': 'ml',
    'mr': 'mr', 'gu': 'gu', 'ur': 'ur'
}

# ============================================================
# LibreTranslate Translation with Retry
# ============================================================

async def translate_chunk_libretranslate(session, text, src, tgt, lt_url, job_id=None):
    """Translate text using LibreTranslate API."""
    src_lt = LIBRETRANSLATE_LANG_MAP.get(src, src)
    tgt_lt = LIBRETRANSLATE_LANG_MAP.get(tgt, tgt)
    payload = {"q": text, "source": src_lt, "target": tgt_lt, "format": "text"}
    prompt_repr = f"POST {lt_url}/translate\n{json.dumps(payload, ensure_ascii=False)}"
    last_err = None
    start = time.time()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(f"{lt_url}/translate", json=payload, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {err[:200]}")
                data = await resp.json()
                result = data.get("translatedText", "").strip()
                if not result:
                    raise Exception("Empty response from LibreTranslate")
                record_exchange(job_id, "libretranslate", None, src, tgt,
                                "libretranslate", prompt_repr, result, attempt,
                                time.time() - start)
                return result, attempt
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)
    record_exchange(job_id, "libretranslate", None, src, tgt, "libretranslate",
                    prompt_repr, "", MAX_RETRIES, time.time() - start, error=str(last_err))
    raise Exception(f"LibreTranslate failed after {MAX_RETRIES} attempts: {last_err}")

# ============================================================
# Ollama Translation with Retry
# ============================================================

MARKER_INSTRUCTION = (
    "If the text contains inline position markers like ⟦0⟧, ⟦1⟧, keep every "
    "marker exactly as-is, immediately before the same word/segment it precedes. "
    "Do not add, remove, renumber, reorder, or translate the markers.\n"
)

async def translate_chunk(session, text, src, tgt, model, url,
                          num_ctx=DEFAULT_NUM_CTX, context="",
                          keep_alive=DEFAULT_KEEP_ALIVE, job_id=None,
                          original_prompt=False):
    src_name = LANG_EN.get(src, src)
    tgt_name = LANG_EN.get(tgt, tgt)
    if original_prompt:
        # Native TranslateGemma prompt (see ollama.com/library/translategemma):
        # includes the language codes, the "Your goal..." sentence and the two
        # blank lines before the text. No inline-marker instruction, so inline
        # formatting is NOT preserved in this mode (text is sent untagged).
        prompt = (
            f"You are a professional {src_name} ({src}) to {tgt_name} ({tgt}) translator. "
            f"Your goal is to accurately convey the meaning and nuances of the original "
            f"{src_name} text while adhering to {tgt_name} grammar, vocabulary, and cultural "
            f"sensitivities.\n"
            f"Produce only the {tgt_name} translation, without any additional explanations "
            f"or commentary. Please translate the following {src_name} text into {tgt_name}:\n\n\n{text}"
        )
    else:
        ctx = ""
        if context:
            ctx = f"\n\nFor context, the previous translated passage was:\n{context[-400:]}\n"
        prompt = (
            f"You are a professional {src_name} to {tgt_name} translator. "
            f"Produce only the {tgt_name} translation, no explanations or commentary.\n"
            f"{MARKER_INSTRUCTION}"
            f"{ctx}\n"
            f"Translate the following {src_name} text into {tgt_name}:\n\n{text}"
        )
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"num_ctx": num_ctx}, "keep_alive": keep_alive}
    last_err = None
    start = time.time()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(f"{url}/api/generate", json=payload, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {err[:200]}")
                data = await resp.json()
                result = data.get("response", "").strip()
                if not result:
                    raise Exception("Empty response from model")
                record_exchange(job_id, "ollama", model, src, tgt, "single",
                                prompt, result, attempt, time.time() - start)
                return result, attempt
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)
    record_exchange(job_id, "ollama", model, src, tgt, "single",
                    prompt, "", MAX_RETRIES, time.time() - start, error=str(last_err))
    raise Exception(f"Failed after {MAX_RETRIES} attempts: {last_err}")


BATCH_SEPARATOR = "\n---SEGMENT_BREAK---\n"

async def translate_batch_ollama(session, texts, src, tgt, model, url,
                                 num_ctx=DEFAULT_NUM_CTX, keep_alive=DEFAULT_KEEP_ALIVE,
                                 job_id=None):
    """Translate multiple text segments in a single Ollama request using separators."""
    src_name = LANG_EN.get(src, src)
    tgt_name = LANG_EN.get(tgt, tgt)
    combined = BATCH_SEPARATOR.join(texts)
    prompt = (
        f"You are a professional {src_name} to {tgt_name} translator. "
        f"Produce only the {tgt_name} translation, no explanations or commentary.\n"
        f"The text below contains {len(texts)} segments separated by '---SEGMENT_BREAK---'.\n"
        f"Translate each segment and keep the same '---SEGMENT_BREAK---' separators in your output.\n"
        f"Do NOT add numbering, labels, or any extra text.\n"
        f"{MARKER_INSTRUCTION}\n"
        f"Translate the following {src_name} text into {tgt_name}:\n\n{combined}"
    )
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"num_ctx": num_ctx}, "keep_alive": keep_alive}
    last_err = None
    start = time.time()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(f"{url}/api/generate", json=payload, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {err[:200]}")
                data = await resp.json()
                result = data.get("response", "").strip()
                if not result:
                    raise Exception("Empty response from model")
                record_exchange(job_id, "ollama", model, src, tgt, "batch",
                                prompt, result, attempt, time.time() - start)
                # Split result back into segments
                parts = re.split(r'\s*---SEGMENT_BREAK---\s*', result)
                if len(parts) != len(texts) or any(not part.strip() for part in parts):
                    raise ValueError(f"Invalid batch result: expected {len(texts)} nonempty segments, got {len(parts)}")
                return [p.strip() for p in parts], attempt
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)
    record_exchange(job_id, "ollama", model, src, tgt, "batch",
                    prompt, "", MAX_RETRIES, time.time() - start, error=str(last_err))
    raise Exception(f"Batch failed after {MAX_RETRIES} attempts: {last_err}")

# ============================================================
# Translation Pipeline
# ============================================================

async def run_pipeline(job: TranslationJob):
    try:
        job.started_at = datetime.now().isoformat()
        if job.engine == 'libretranslate':
            job.libretranslate_url = validate_engine_url(job.libretranslate_url, 'libretranslate')
        else:
            job.ollama_url = validate_engine_url(job.ollama_url, 'ollama')
        filepath = UPLOAD_DIR / f"{job.job_id}_{job.filename}"
        ext = filepath.suffix.lower()
        is_docx = ext == '.docx'
        is_pdf = ext == '.pdf'
        pdf_converted_docx = None  # Temp DOCX path if PDF was converted
        use_docx_pipeline = is_docx  # Will be set True for PDF too

        # --- EXTRACT ---
        job.status = JobStatus.EXTRACTING
        job.current_step = f"Extracting text from {job.filename}..."
        job.progress = 5
        job.add_event("info", "Extraction started", job.filename)
        emit(job)

        if is_pdf:
            # PDF: convert to DOCX first, then use format-preserving pipeline
            job.current_step = "Analyzing PDF type (digital vs scanned)..."
            emit(job)

            pdf_converted_docx = UPLOAD_DIR / f"{job.job_id}_converted.docx"
            ocr_lang = TESS_LANG_MAP.get(job.source_lang, 'eng')
            if ocr_lang != 'eng':
                ocr_lang += '+eng'
            job.current_step = "Converting PDF pages (digital text and OCR)..."
            emit(job)
            # A failed page must fail the job; plain-text fallback could silently
            # discard scanned pages from an otherwise digital document.
            prepare_pdf_for_translation(filepath, pdf_converted_docx, lang=ocr_lang)
            filepath = pdf_converted_docx
            job.original_docx_path = str(pdf_converted_docx)
            use_docx_pipeline = True
            job.add_event("success", "PDF converted", "Every page processed")

        if use_docx_pipeline:
            # Format-preserving DOCX pipeline (for DOCX and converted PDF)
            try:
                segments = extract_docx_segments(filepath, tag_inline=not job.original_prompt)
            except Exception as e:
                raise Exception(f"DOCX extraction failed: {e}")
            if not segments:
                raise Exception("No text content found in document")

            job.docx_segments = segments
            job.total_chars = sum(s["char_count"] for s in segments)
            job.total_chunks = len(segments)
            job.translated_chunks = [""] * len(segments)
            mode_label = "PDF->DOCX format-preserving" if is_pdf else "DOCX format-preserving"
            job.add_event("success", f"Extraction complete ({mode_label})",
                          f"{len(segments)} segments, {job.total_chars:,} chars")
        elif not is_docx:
            # Plain text pipeline (TXT or PDF fallback)
            try:
                if is_pdf:
                    paragraphs = extract_text_from_pdf(filepath)
                else:
                    paragraphs = extract_text_from_txt(filepath)
            except Exception as e:
                raise Exception(f"Extraction failed: {e}")
            if not paragraphs:
                raise Exception("No text content found in document")

            job.total_chars = sum(len(p) for p in paragraphs)
            job.add_event("success", "Extraction complete (plain text)",
                          f"{len(paragraphs)} paragraphs, {job.total_chars:,} chars")

        emit(job)

        # --- CHUNK (only for non-DOCX) ---
        if use_docx_pipeline:
            # For DOCX (and converted PDF), each segment is a chunk
            chunks_to_translate = [{"index": s["index"], "text": s["text"],
                                    "char_count": s["char_count"]} for s in segments]
            job.add_event("info", "Using format-preserving mode",
                          f"{len(chunks_to_translate)} segments (1 per paragraph/cell)")
        else:
            job.status = JobStatus.CHUNKING
            job.current_step = "Splitting into translation chunks..."
            job.progress = 10
            emit(job)
            chunks_to_translate = create_chunks(paragraphs, max_chars=job.chunk_size)
            job.total_chunks = len(chunks_to_translate)
            job.translated_chunks = [""] * len(chunks_to_translate)
            job.add_event("info", "Chunking complete",
                          f"{len(chunks_to_translate)} chunks (max {job.chunk_size} chars/chunk)")

        # Keep clean source text per segment for the optional LLM review step
        job.source_segments = [INLINE_MARKER_RE.sub("", c["text"]) for c in chunks_to_translate]

        # Store original file reference for review revised-document generation
        # (the original uploaded file is at UPLOAD_DIR/{job_id}_{filename})
        job.original_filename = job.filename
        if use_docx_pipeline:
            job.original_docx_segments = segments

        engine_info = f"engine={job.engine}"
        if job.engine == 'libretranslate':
            engine_info += f", url={job.libretranslate_url}"
        else:
            engine_info += f", model={job.model}, num_ctx={job.num_ctx}, batch={job.batch_size}"
        job.add_event("info", "Pipeline config",
                      f"concurrency={job.concurrency}, keep_alive={job.keep_alive}, {engine_info}")
        emit(job)

        # --- TRANSLATE (parallel) ---
        job.status = JobStatus.TRANSLATING
        job.progress = 12
        emit(job)

        use_libretranslate = (job.engine == 'libretranslate')

        async with aiohttp.ClientSession() as session:
            # Verify connection to translation engine
            if use_libretranslate:
                try:
                    async with session.get(f"{job.libretranslate_url}/languages", allow_redirects=False,
                                           timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status != 200:
                            raise Exception(f"HTTP {r.status}")
                except Exception as e:
                    raise Exception(f"Cannot connect to LibreTranslate at {job.libretranslate_url}: {e}")
                job.add_event("success", "LibreTranslate connected", job.libretranslate_url)
            else:
                try:
                    async with session.get(f"{job.ollama_url}/api/tags", allow_redirects=False,
                                           timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status != 200:
                            raise Exception(f"HTTP {r.status}")
                except Exception as e:
                    raise Exception(f"Cannot connect to Ollama at {job.ollama_url}: {e}")
                job.add_event("success", "Ollama connected", job.ollama_url)

            semaphore = asyncio.Semaphore(job.concurrency)
            lock = asyncio.Lock()
            total_segs = len(chunks_to_translate)

            async def translate_segment(chunk):
                parts = split_text_bounded(chunk["text"], job.chunk_size)
                results = []
                attempts = 1
                for part in parts:
                    if job.cancelled:
                        raise asyncio.CancelledError
                    if use_libretranslate:
                        translated, count = await translate_chunk_libretranslate(
                            session, part, job.source_lang, job.target_lang,
                            job.libretranslate_url, job_id=job.job_id)
                    else:
                        translated, count = await translate_chunk(
                            session, part, job.source_lang, job.target_lang,
                            job.model, job.ollama_url, num_ctx=job.num_ctx,
                            keep_alive=job.keep_alive, job_id=job.job_id,
                            original_prompt=job.original_prompt)
                    # Restore only separator whitespace at the split boundary;
                    # each engine is free to translate the words in its fragment.
                    separator = re.search(r"\s+$", part)
                    results.append(translated + (separator.group() if separator else ""))
                    attempts = max(attempts, count)
                return "".join(results), attempts

            # Decide whether to use batching (Ollama only, batch_size > 1)
            # Batching adds segment-separator instructions to the prompt, which would
            # break the native "original prompt" — so it's disabled in that mode.
            use_batching = ((not use_libretranslate) and job.batch_size > 1
                            and not job.original_prompt
                            and all(len(c["text"]) <= job.chunk_size for c in chunks_to_translate))

            if use_batching:
                # --- BATCH MODE (Ollama) ---
                batch_sz = job.batch_size
                batches = []
                pending = []
                pending_chars = 0
                batch_start = 0
                for index, chunk in enumerate(chunks_to_translate):
                    added_chars = len(chunk["text"]) + (len(BATCH_SEPARATOR) if pending else 0)
                    if pending and (len(pending) >= batch_sz or pending_chars + added_chars > job.chunk_size):
                        batches.append((batch_start, pending))
                        pending, pending_chars, batch_start = [], 0, index
                        added_chars = len(chunk["text"])
                    pending.append(chunk)
                    pending_chars += added_chars
                if pending:
                    batches.append((batch_start, pending))
                batch_numbers = {start: number + 1 for number, (start, _) in enumerate(batches)}
                job.add_event("info", "Batch mode enabled",
                              f"{len(batches)} batches of up to {batch_sz} segments")
                emit(job)

                async def translate_batch(batch_start, batch_chunks):
                    if job.cancelled:
                        return
                    async with semaphore:
                        if job.cancelled:
                            return
                        indices = list(range(batch_start, batch_start + len(batch_chunks)))
                        batch_num = batch_numbers[batch_start]
                        total_batch_chars = sum(c["char_count"] for c in batch_chunks)
                        async with lock:
                            job.current_step = f"[Ollama] Batch {batch_num}/{len(batches)} ({len(batch_chunks)} seg, {total_batch_chars} chars)..."
                            job.progress = 12 + (78 * job.completed_chunks / total_segs)
                            emit(job)
                        try:
                            texts = [c["text"] for c in batch_chunks]
                            results, attempts = await translate_batch_ollama(
                                session, texts, job.source_lang, job.target_lang,
                                job.model, job.ollama_url, num_ctx=job.num_ctx,
                                keep_alive=job.keep_alive, job_id=job.job_id
                            )
                            async with lock:
                                for k, (idx, chunk) in enumerate(zip(indices, batch_chunks)):
                                    translated = results[k] if k < len(results) else ""
                                    job.translated_chunks[idx] = translated
                                    job.completed_chunks += 1
                                    job.translated_chars += len(translated)
                                if attempts > 1:
                                    job.retried_chunks += 1
                                    job.add_event("warning", f"Batch {batch_num}/{len(batches)} OK after {attempts} attempts",
                                                  f"{total_batch_chars}\u2192{sum(len(r) for r in results)} chars")
                                else:
                                    job.add_event("success", f"Batch {batch_num}/{len(batches)} translated",
                                                  f"{total_batch_chars}\u2192{sum(len(r) for r in results)} chars")
                                job.progress = 12 + (78 * job.completed_chunks / total_segs)
                                emit(job)
                        except Exception as e:
                            # Fallback: translate individually
                            async with lock:
                                job.add_event("warning", f"Batch {batch_num} failed, falling back to individual", str(e))
                                emit(job)
                            for k, (idx, chunk) in enumerate(zip(indices, batch_chunks)):
                                if job.cancelled:
                                    return
                                try:
                                    translated, att = await translate_segment(chunk)
                                    job.translated_chunks[idx] = translated
                                    async with lock:
                                        job.completed_chunks += 1
                                        job.translated_chars += len(translated)
                                        job.progress = 12 + (78 * job.completed_chunks / total_segs)
                                        job.add_event("success", f"Segment {idx+1}/{total_segs} translated (fallback)",
                                                      f"{chunk['char_count']}\u2192{len(translated)} chars")
                                        emit(job)
                                except Exception as e2:
                                    async with lock:
                                        job.failed_chunks += 1
                                        job.completed_chunks += 1
                                        job.translated_chunks[idx] = f"[TRANSLATION ERROR: {e2}]"
                                        job.progress = 12 + (78 * job.completed_chunks / total_segs)
                                        job.add_event("error", f"Segment {idx+1}/{total_segs} FAILED", str(e2))
                                        emit(job)

                tasks = [translate_batch(bi, bc) for bi, bc in batches]
                await asyncio.gather(*tasks)

            else:
                # --- INDIVIDUAL MODE (LibreTranslate or batch_size=1) ---
                async def translate_one(i, chunk):
                    if job.cancelled:
                        return
                    async with semaphore:
                        if job.cancelled:
                            return
                        n = i + 1
                        async with lock:
                            engine_label = "LibreTranslate" if use_libretranslate else "Ollama"
                            job.current_step = f"[{engine_label}] Translating segment {n}/{total_segs} ({chunk['char_count']} chars)..."
                            job.progress = 12 + (78 * job.completed_chunks / total_segs)
                            emit(job)
                        try:
                            translated, attempts = await translate_segment(chunk)
                            job.translated_chunks[i] = translated
                            async with lock:
                                job.completed_chunks += 1
                                job.translated_chars += len(translated)
                                job.progress = 12 + (78 * job.completed_chunks / total_segs)
                                if attempts > 1:
                                    job.retried_chunks += 1
                                    job.add_event("warning", f"Segment {n}/{total_segs} OK after {attempts} attempts",
                                                  f"{chunk['char_count']}\u2192{len(translated)} chars")
                                else:
                                    job.add_event("success", f"Segment {n}/{total_segs} translated",
                                                  f"{chunk['char_count']}\u2192{len(translated)} chars")
                                emit(job)
                        except Exception as e:
                            async with lock:
                                job.failed_chunks += 1
                                job.completed_chunks += 1
                                job.translated_chunks[i] = f"[TRANSLATION ERROR: {e}]"
                                job.progress = 12 + (78 * job.completed_chunks / total_segs)
                                job.add_event("error", f"Segment {n}/{total_segs} FAILED", str(e))
                                emit(job)

                tasks = [translate_one(i, chunk) for i, chunk in enumerate(chunks_to_translate)]
                await asyncio.gather(*tasks)

            if job.cancelled:
                job.status = JobStatus.CANCELLED
                job.current_step = "Cancelled by user"
                job.add_event("warning", "Job cancelled by user")
                job.completed_at = datetime.now().isoformat()
                emit(job)
                return

        if job.failed_chunks:
            raise RuntimeError(f"Traducere incompletă: {job.failed_chunks}/{job.total_chunks} segmente au eșuat. Verifică motorul și modelul; detaliile sunt în evenimentele jobului.")

        # --- ASSEMBLE ---
        job.status = JobStatus.ASSEMBLING
        job.current_step = "Assembling translated document..."
        job.progress = 92
        emit(job)

        if use_docx_pipeline:
            out_name = Path(job.filename).stem + f"_{job.target_lang}.docx"
            output_path = OUTPUT_DIR / f"{job.job_id}_{out_name}"
            apply_translations_to_docx(filepath, segments, job.translated_chunks, output_path)
            label = "PDF->DOCX assembled (format preserved)" if is_pdf else "DOCX assembled (format preserved)"
            job.add_event("success", label, out_name)
            # Keep converted structure for review; job deletion/retention removes it.
        else:
            out_name = Path(job.filename).stem + f"_{job.target_lang}"
            if ext == '.pdf':
                out_name += '.txt'
            else:
                out_name += ext
            assemble_txt(job, out_name)
            job.add_event("success", "Document assembled", out_name)

        job.output_file = out_name

        # --- CONVERT TO PDF (optional, LibreOffice headless) ---
        if job.convert_to_pdf and out_name.endswith('.docx'):
            try:
                job.current_step = "Converting to PDF (LibreOffice)..."
                job.progress = 96
                emit(job)
                docx_output_path = OUTPUT_DIR / f"{job.job_id}_{out_name}"
                pdf_path = convert_docx_to_pdf_libreoffice(docx_output_path, OUTPUT_DIR)
                # Rename to include job_id prefix
                pdf_name = Path(job.filename).stem + f"_{job.target_lang}.pdf"
                final_pdf = OUTPUT_DIR / f"{job.job_id}_{pdf_name}"
                if pdf_path != final_pdf:
                    pdf_path.rename(final_pdf)
                # Remove DOCX, keep only PDF
                if docx_output_path.exists():
                    docx_output_path.unlink()
                job.output_file = pdf_name
                job.add_event("success", "Converted to PDF", pdf_name)
            except Exception as e:
                job.add_event("warning", "PDF conversion failed, keeping DOCX", str(e))
                app_logger.warning(f"LibreOffice conversion failed: {e}")

        # --- DONE ---
        job.status = JobStatus.COMPLETED
        job.progress = 100
        job.completed_at = datetime.now().isoformat()
        job.current_step = "Complete!"
        job.add_event("success", "Translation complete",
                      f"{job.completed_chunks}/{job.total_chunks} segments, "
                      f"{job.failed_chunks} failures, {job.retried_chunks} retries")
        emit(job)

    except asyncio.CancelledError:
        job.cancelled = True
        job.status = JobStatus.CANCELLED
        job.current_step = "Cancelled by user"
        job.completed_at = datetime.now().isoformat()
        job.add_event("warning", "Job cancelled by user")
        emit(job)
        raise
    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.current_step = f"Failed: {e}"
        job.completed_at = datetime.now().isoformat()
        job.add_event("error", "Pipeline failed", str(e))
        emit(job)

def assemble_txt(job, out_name):
    path = OUTPUT_DIR / f"{job.job_id}_{out_name}"
    with open(path, 'w', encoding='utf-8') as f:
        for translated in job.translated_chunks:
            if translated:
                f.write(translated + "\n\n")

# ============================================================
# LLM Translation Review (second-pass quality check via Ollama)
# ============================================================

def _parse_review_json(raw: str) -> dict:
    """Robustly parse the reviewer model's JSON output."""
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        # Try to extract the first {...} block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return {"verdict": "unknown", "score": None, "issues": [],
                "suggestion": "", "raw": raw[:500]}
    verdict = str(data.get("verdict", "unknown")).lower().strip()
    if verdict not in ("ok", "minor", "major"):
        verdict = "unknown"
    issues = data.get("issues", [])
    if isinstance(issues, str):
        issues = [issues]
    elif not isinstance(issues, list):
        issues = []
    issues = [str(i).strip() for i in issues if str(i).strip()]
    score = data.get("score")
    try:
        score = int(score) if score is not None else None
    except Exception:
        score = None
    suggestion = data.get("suggestion") or data.get("improved") or ""
    return {"verdict": verdict, "score": score,
            "issues": issues, "suggestion": str(suggestion).strip()}


async def review_segment(session, src_text, tgt_text, src, tgt, model, url,
                         num_ctx=DEFAULT_NUM_CTX, keep_alive=DEFAULT_KEEP_ALIVE,
                         job_id=None, preserve_markers=False,
                         context_before="", context_after="",
                         context_before_tgt="", context_after_tgt=""):
    """Ask a general LLM to review one source/translation pair. Returns a dict."""
    src_name = LANG_EN.get(src, src)
    tgt_name = LANG_EN.get(tgt, tgt)
    marker_note = ""
    if preserve_markers:
        marker_note = (
            "The TRANSLATION contains inline position markers like ⟦0⟧, ⟦1⟧ that mark "
            "text-formatting boundaries (bold/italic/etc.). If you provide a suggestion, "
            "you MUST keep all of these markers in it, repositioned so each marked span "
            "still matches the corresponding words. Do not add, remove, or renumber markers.\n"
        )
    context_note = ""
    if context_before or context_after or context_before_tgt or context_after_tgt:
        lines = []
        if context_before:
            lines.append(f"BEFORE — {src_name}: {context_before}")
        if context_before_tgt:
            lines.append(f"BEFORE — {tgt_name}: {context_before_tgt}")
        if context_after:
            lines.append(f"AFTER — {src_name}: {context_after}")
        if context_after_tgt:
            lines.append(f"AFTER — {tgt_name}: {context_after_tgt}")
        context_note = (
            f"The following surrounding text is given as CONTEXT ONLY, to help you resolve "
            f"references, terminology and tone. Do NOT review or translate it; judge ONLY "
            f"the SOURCE/TRANSLATION pair below.\n"
            + "\n".join(lines) + "\n\n"
        )
    prompt = (
        f"You are a senior bilingual translation reviewer ({src_name} into {tgt_name}). "
        f"Compare the SOURCE with its TRANSLATION and judge the translation's quality: "
        f"accuracy, completeness (no omissions/additions), terminology, grammar, "
        f"and natural fluency in {tgt_name}.\n"
        f"Respond ONLY with a JSON object, no other text, using exactly these keys:\n"
        f'{{"verdict": "ok" | "minor" | "major", "score": <integer 1-5>, '
        f'"issues": [<short strings describing problems, in {tgt_name}>], '
        f'"suggestion": "<an improved {tgt_name} translation, or empty string if none needed>"}}\n'
        f'Use "ok" when the translation is faithful and natural, "minor" for small '
        f'style/grammar issues, "major" for meaning errors or omissions. '
        f'Leave "suggestion" empty when verdict is "ok".\n'
        f"{marker_note}"
        f"{context_note}"
        f"SOURCE ({src_name}):\n{src_text}\n\n"
        f"TRANSLATION ({tgt_name}):\n{tgt_text}"
    )
    payload = {"model": model, "prompt": prompt, "stream": False, "format": "json",
               "options": {"num_ctx": num_ctx, "temperature": 0}, "keep_alive": keep_alive}
    last_err = None
    start = time.time()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(f"{url}/api/generate", json=payload, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {err[:200]}")
                data = await resp.json()
                raw = data.get("response", "").strip()
                if not raw:
                    raise Exception("Empty response from model")
                record_exchange(job_id, "ollama", model, src, tgt, "review",
                                prompt, raw, attempt, time.time() - start)
                parsed = _parse_review_json(raw)
                parsed["attempts"] = attempt
                return parsed
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)
    record_exchange(job_id, "ollama", model, src, tgt, "review",
                    prompt, "", MAX_RETRIES, time.time() - start, error=str(last_err))
    raise Exception(f"Review failed after {MAX_RETRIES} attempts: {last_err}")


def _review_summary(results_list, partial=False):
    """Aggregate per-segment review verdicts into a summary dict."""
    ok = sum(1 for r in results_list if r["verdict"] == "ok")
    minor = sum(1 for r in results_list if r["verdict"] == "minor")
    major = sum(1 for r in results_list if r["verdict"] == "major")
    errors = sum(1 for r in results_list if r["verdict"] in ("error", "unknown"))
    scores = [r["score"] for r in results_list if isinstance(r["score"], int)]
    avg_score = round(sum(scores) / len(scores), 2) if scores else None
    return {"ok": ok, "minor": minor, "major": major, "errors": errors,
            "avg_score": avg_score, "total": len(results_list), "partial": partial}


def _reset_review(job: TranslationJob, model: str):
    """Reserve the job immediately and clear results from an earlier review."""
    job.review_status = "running"
    job.review_model = model
    job.review_error = None
    job.review_cancel = False
    job.review_results = []
    job.review_summary = None
    job.review_done = 0
    job.review_total = 0
    job.review_progress = 0.0
    job.revised_output_file = None
    job.review_export_file = None


async def run_review(job: TranslationJob, model: str, url: str,
                     num_ctx: int = DEFAULT_NUM_CTX, concurrency: int = DEFAULT_CONCURRENCY,
                     keep_alive: str = DEFAULT_KEEP_ALIVE, context_window: int = 1,
                     context_translations: bool = False, _prepared: bool = False):
    """Review every translated segment of a completed job with a general LLM.

    ``context_window`` is how many neighbouring source segments (each side) are
    passed to the reviewer as read-only context so it can judge references,
    terminology and tone in context. 0 disables surrounding context.
    When ``context_translations`` is set, the neighbouring *translations* are
    included alongside the source context.
    """
    try:
        if not _prepared:
            _reset_review(job, model)

        # Build aligned (index, source, translation) pairs, skipping empties/errors.
        # For segments with inline formatting markers (⟦n⟧), the reviewer is shown
        # the marked translation and asked to keep the markers, so an applied
        # suggestion can preserve bold/italic exactly like the original translation.
        sources = job.source_segments or []
        # Pre-clean every source/translation segment once so we can cheaply
        # assemble the surrounding context for any index.
        clean_sources = [INLINE_MARKER_RE.sub("", s or "").strip() for s in sources]
        clean_trans_all = [INLINE_MARKER_RE.sub("", t or "").strip()
                           for t in job.translated_chunks]
        CTX_CHAR_BUDGET = 600  # per side, to keep the prompt within num_ctx

        def _join(seq, lo, hi):
            return " ".join(seq[j] for j in range(max(0, lo), min(len(seq), hi)) if seq[j])

        def context_for(i):
            if context_window <= 0:
                return "", "", "", ""
            before = _join(clean_sources, i - context_window, i)[-CTX_CHAR_BUDGET:]
            after = _join(clean_sources, i + 1, i + 1 + context_window)[:CTX_CHAR_BUDGET]
            before_tgt = after_tgt = ""
            if context_translations:
                before_tgt = _join(clean_trans_all, i - context_window, i)[-CTX_CHAR_BUDGET:]
                after_tgt = _join(clean_trans_all, i + 1, i + 1 + context_window)[:CTX_CHAR_BUDGET]
            return before, after, before_tgt, after_tgt

        pairs = []
        for i, trans in enumerate(job.translated_chunks):
            raw_trans = trans or ""
            clean_trans = INLINE_MARKER_RE.sub("", raw_trans).strip()
            src_text = clean_sources[i] if i < len(clean_sources) else ""
            if not clean_trans or clean_trans.startswith("[TRANSLATION ERROR"):
                continue
            if not src_text:
                continue
            has_markers = bool(INLINE_MARKER_RE.search(raw_trans))
            tgt_for_review = raw_trans.strip() if has_markers else clean_trans
            ctx_before, ctx_after, ctx_before_tgt, ctx_after_tgt = context_for(i)
            pairs.append((i, src_text, clean_trans, tgt_for_review, has_markers,
                          ctx_before, ctx_after, ctx_before_tgt, ctx_after_tgt))

        job.review_total = len(pairs)
        if not pairs:
            job.review_status = "failed"
            job.review_error = "Nu există segmente traduse pentru verificare."
            emit(job)
            return

        job.add_event("info", "Verificare LLM pornită",
                      f"{len(pairs)} segmente, model={model}")
        emit(job)

        semaphore = asyncio.Semaphore(max(1, concurrency))
        lock = asyncio.Lock()
        results = {}

        async with aiohttp.ClientSession() as session:
            # Verify model/endpoint reachable
            try:
                async with session.get(f"{url}/api/tags", allow_redirects=False,
                                       timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status != 200:
                        raise Exception(f"HTTP {r.status}")
            except Exception as e:
                job.review_status = "failed"
                job.review_error = f"Nu mă pot conecta la Ollama ({url}): {e}"
                job.add_event("error", "Verificare eșuată", job.review_error)
                emit(job)
                return

            async def review_one(i, src_text, disp_trans, tgt_for_review, has_markers,
                                 ctx_before, ctx_after, ctx_before_tgt, ctx_after_tgt):
                if job.review_cancel:
                    return
                async with semaphore:
                    if job.review_cancel:
                        return
                    try:
                        parsed = await review_segment(
                            session, src_text, tgt_for_review, job.source_lang,
                            job.target_lang, model, url, num_ctx=num_ctx,
                            keep_alive=keep_alive, job_id=job.job_id,
                            preserve_markers=has_markers,
                            context_before=ctx_before, context_after=ctx_after,
                            context_before_tgt=ctx_before_tgt,
                            context_after_tgt=ctx_after_tgt)
                    except Exception as e:
                        parsed = {"verdict": "error", "score": None, "issues": [str(e)],
                                  "suggestion": ""}
                    raw_sugg = (parsed.get("suggestion") or "").strip()
                    async with lock:
                        results[i] = {
                            "index": i,
                            "source": src_text,
                            "translation": disp_trans,
                            "verdict": parsed.get("verdict", "unknown"),
                            "score": parsed.get("score"),
                            "issues": parsed.get("issues", []),
                            # display version (markers stripped) + raw version (markers kept)
                            "suggestion": INLINE_MARKER_RE.sub("", raw_sugg).strip(),
                            "suggestion_raw": raw_sugg,
                        }
                        job.review_done += 1
                        job.review_progress = 100.0 * job.review_done / job.review_total
                        # Expose partial results live so the UI populates in real time
                        job.review_results = [results[k] for k in sorted(results.keys())]
                        job.review_summary = _review_summary(job.review_results, partial=True)
                        emit(job)

            await asyncio.gather(*[review_one(*p) for p in pairs])

        if job.review_cancel:
            job.review_status = "cancelled"
            job.add_event("warning", "Verificare anulată")
            emit(job)
            return

        job.review_results = [results[k] for k in sorted(results.keys())]
        job.review_summary = _review_summary(job.review_results)
        sm = job.review_summary
        if sm["total"] and sm["errors"] == sm["total"]:
            first_error = next((issue for result in job.review_results
                                for issue in result.get("issues", []) if issue), "")
            raise RuntimeError(f"Verificarea a eșuat pentru toate segmentele. {first_error}")
        job.review_status = "completed"
        job.review_progress = 100.0
        job.add_event("success", "Verificare finalizată",
                      f"{sm['ok']} ok · {sm['minor']} minore · {sm['major']} majore"
                      + (f" · scor mediu {sm['avg_score']}/5" if sm['avg_score'] else ""))
        emit(job)
    except Exception as e:
        job.review_status = "failed"
        job.review_error = str(e)
        job.add_event("error", "Verificare eșuată", str(e))
        emit(job)


def build_revised_document(job: TranslationJob, revised_translations: list[str]):
    """Apply revised body/table text to the document whose structure it describes.

    External verification edits the uploaded translation, never the source-language
    original. Untouched/unmatched translated paragraphs remain intact. Ordinary
    translation jobs rebuild from their original DOCX with all translated segments.
    """
    if job.kind == "verify":
        orig_filename = job.filename
        orig_docx_segments = job.docx_segments
    else:
        orig_filename = job.original_filename or job.filename
        orig_docx_segments = job.original_docx_segments or job.docx_segments
    original_path = Path(getattr(job, "original_docx_path", "") or (UPLOAD_DIR / f"{job.job_id}_{orig_filename}"))
    if getattr(job, 'original_docx_path', None) and not original_path.exists():
        raise FileNotFoundError('Documentul de bază pentru revizuire lipsește. Reîncarcă documentul înainte de a aplica modificările.')
    orig_ext = Path(orig_filename).suffix.lower()
    if orig_ext == '.docx' and (not original_path.exists() or not orig_docx_segments):
        raise FileNotFoundError('Documentul DOCX de bază sau structura sa lipsește. Reîncarcă documentul înainte de revizuire.')
    stem = Path(orig_filename).stem

    if original_path.suffix.lower() == '.docx' and orig_docx_segments and original_path.exists():
        out_name = f"{stem}_{job.target_lang}_revizuit_review.docx"
        out_path = OUTPUT_DIR / f"{job.job_id}_rev_{out_name}"
        translations = []
        for seg in orig_docx_segments:
            idx = seg["index"]
            text = revised_translations[idx] if idx < len(revised_translations) else None
            if (job.kind == "verify" and idx < len(job.translated_chunks)
                    and text == job.translated_chunks[idx]):
                text = None  # Keep the untouched translated paragraph XML intact.
            translations.append(text)
        apply_translations_to_docx(original_path, orig_docx_segments, translations,
                                   out_path, allow_empty=True)
        return out_path, out_name

    # Plain text source
    if orig_ext in ('.txt', '.text', '.md'):
        out_name = f"{stem}_{job.target_lang}_revizuit_review.txt"
        out_path = OUTPUT_DIR / f"{job.job_id}_rev_{out_name}"
        with open(out_path, 'w', encoding='utf-8') as f:
            for t in revised_translations:
                clean = INLINE_MARKER_RE.sub("", t or "")
                if clean.strip():
                    f.write(clean + "\n\n")
        return out_path, out_name

    # Fallback (e.g. PDF source whose converted DOCX is gone): plain DOCX, text only
    out_name = f"{stem}_{job.target_lang}_revizuit_review.docx"
    out_path = OUTPUT_DIR / f"{job.job_id}_rev_{out_name}"
    d = docx.Document()
    for t in revised_translations:
        clean = INLINE_MARKER_RE.sub("", t or "")
        if clean.strip():
            d.add_paragraph(clean)
    d.save(str(out_path))
    return out_path, out_name

# ============================================================
# SSE
# ============================================================

def emit(job: TranslationJob):
    jid = job.job_id
    if jid in sse_queues:
        data = job.to_dict()
        for q in sse_queues[jid]:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass
    asyncio.ensure_future(broadcast_dashboard({"type": "job_update", "data": job.to_dict()}))

# ============================================================
# API Endpoints
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text(encoding='utf-8'))

@app.get("/api/status")
async def api_status():
    return await get_dashboard_data()

@app.get("/healthz")
async def healthz():
    """Public minimal readiness: no prompts, filenames, URLs or secret values."""
    ready = False
    if configured_password():
        if DEFAULT_ENGINE == "libretranslate":
            status = await api_libretranslate(DEFAULT_LIBRETRANSLATE_URL)
            installed = {entry["code"] for entry in status.get("languages", [])}
            ready = status.get("connected", False) and set(_get_installed_languages()) <= installed
        else:
            status = await get_ollama_status(DEFAULT_OLLAMA_URL)
            models = {entry.get("name", "") for entry in status.get("models", [])}
            ready = status.get("connected", False) and (DEFAULT_MODEL in models or DEFAULT_MODEL + ":latest" in models)
    return JSONResponse({"ready": bool(ready)}, status_code=200 if ready else 503)

@app.get("/api/ollama")
async def api_ollama(url: str = DEFAULT_OLLAMA_URL):
    url = validate_engine_url(url)
    return await get_ollama_status(url)

@app.get("/api/models")
async def api_models(url: str = DEFAULT_OLLAMA_URL):
    url = validate_engine_url(url)
    result = await get_ollama_status(url)
    return {"connected": result["connected"], "models": result["models"]}

@app.get("/api/libretranslate")
async def api_libretranslate(url: str = DEFAULT_LIBRETRANSLATE_URL):
    """Check LibreTranslate status and available languages."""
    url = validate_engine_url(url, "libretranslate")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{url}/languages",
                                   timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False) as resp:
                if resp.status == 200:
                    languages = await resp.json()
                    if isinstance(languages, list) and languages and all(isinstance(entry, dict) and isinstance(entry.get("code"), str) for entry in languages):
                        # Reflect only the documented language fields, never arbitrary JSON.
                        languages = [{"code": entry["code"], "name": str(entry.get("name", entry["code"]))} for entry in languages]
                        return {"connected": True, "languages": languages, "url": url}
    except:
        pass
    return {"connected": False, "languages": [], "url": url}

@app.post("/api/translate")
async def api_translate(
    request: Request,
    file: UploadFile = File(...),
    source_lang: str = Form("ro"),
    target_lang: str = Form("en"),
    model: str = Form(DEFAULT_MODEL),
    ollama_url: str = Form(""),
    concurrency: int = Form(DEFAULT_CONCURRENCY),
    num_ctx: int = Form(DEFAULT_NUM_CTX),
    engine: str = Form(DEFAULT_ENGINE),
    libretranslate_url: str = Form(DEFAULT_LIBRETRANSLATE_URL),
    convert_to_pdf: str = Form("false"),
    batch_size: int = Form(DEFAULT_BATCH_SIZE),
    keep_alive: str = Form(DEFAULT_KEEP_ALIVE),
    chunk_size: int = Form(MAX_CHUNK_CHARS),
    original_prompt: str = Form("false")
):
    validate_languages(source_lang, target_lang)
    ollama_url = validate_engine_url(ollama_url)
    libretranslate_url = validate_engine_url(libretranslate_url, "libretranslate")
    stored_name = upload_filename(file.filename)
    ext = Path(stored_name).suffix.lower()
    if ext not in ('.docx', '.pdf', '.txt', '.text', '.md'):
        raise HTTPException(400, f"Unsupported: {ext}")
    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)")
    concurrency = max(1, min(concurrency, 10))
    num_ctx = max(512, min(num_ctx, 32768))
    batch_size = max(1, min(batch_size, 10))
    chunk_size = max(500, min(chunk_size, 10000))
    job_id = str(uuid.uuid4())[:8]
    client_ip = request.client.host if request.client else "unknown"
    engine = engine if engine in ('ollama', 'libretranslate') else 'ollama'
    pdf_convert = convert_to_pdf.lower() in ('true', '1', 'yes')
    orig_prompt = original_prompt.lower() in ('true', '1', 'yes')
    job = TranslationJob(job_id, stored_name, source_lang, target_lang, model, ollama_url,
                         client_ip, concurrency=concurrency, num_ctx=num_ctx,
                         engine=engine, libretranslate_url=libretranslate_url,
                         convert_to_pdf=pdf_convert, batch_size=batch_size,
                         keep_alive=keep_alive, chunk_size=chunk_size,
                         original_prompt=orig_prompt)
    job.display_filename = file.filename
    save_path = UPLOAD_DIR / f"{job_id}_{stored_name}"
    try:
        save_path.write_bytes(content)
    except OSError as exc:
        save_path.unlink(missing_ok=True)
        raise HTTPException(500, "Fișierul nu a putut fi salvat.") from exc
    job.add_event("info", "File uploaded", f"{file.filename} ({len(content):,} bytes) from {client_ip}")
    jobs[job_id] = job
    try:
        start_pipeline(job)
    except Exception:
        jobs.pop(job_id, None)
        save_path.unlink(missing_ok=True)
        raise
    return {"job_id": job_id, "status": "queued"}


from pydantic import BaseModel

class TextTranslateRequest(BaseModel):
    text: str
    source_lang: str = "ro"
    target_lang: str = "en"
    engine: str = "ollama"
    model: str = DEFAULT_MODEL
    ollama_url: str = ""
    libretranslate_url: str = DEFAULT_LIBRETRANSLATE_URL
    num_ctx: int = DEFAULT_NUM_CTX
    original_prompt: bool = False

@app.post("/api/translate-text")
async def api_translate_text(req: TextTranslateRequest):
    """Translate text directly (Google Translate style) without creating a job."""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Text is empty")
    if len(text) > 50000:
        raise HTTPException(400, "Text too long (max 50,000 characters)")

    validate_languages(req.source_lang, req.target_lang)
    ollama_url = validate_engine_url(req.ollama_url)
    libretranslate_url = validate_engine_url(req.libretranslate_url, "libretranslate")
    engine = req.engine if req.engine in ('ollama', 'libretranslate') else 'ollama'
    num_ctx = max(512, min(req.num_ctx, 32768))

    app_logger.info(f"Text translate: {len(text)} chars, {req.source_lang}->{req.target_lang}, engine={engine}")

    try:
        async with aiohttp.ClientSession() as session:
            if engine == 'libretranslate':
                # For LibreTranslate, translate directly (handles chunking internally for short text)
                translated, attempts = await translate_chunk_libretranslate(
                    session, text, req.source_lang, req.target_lang, libretranslate_url,
                    job_id="text"
                )
            else:
                # For Ollama, translate directly
                translated, attempts = await translate_chunk(
                    session, text, req.source_lang, req.target_lang,
                    req.model, ollama_url, num_ctx=num_ctx, job_id="text",
                    original_prompt=req.original_prompt
                )

        app_logger.info(f"Text translate complete: {len(translated)} chars, {attempts} attempt(s)")
        return {"translated_text": translated, "engine": engine, "attempts": attempts}

    except Exception as e:
        app_logger.error(f"Text translate error: {e}")
        raise HTTPException(500, f"Translation failed: {str(e)}")

@app.get("/api/jobs")
async def api_jobs():
    return {"jobs": [j.to_dict() for j in sorted(jobs.values(), key=lambda j: j.created_at, reverse=True)]}

@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id].to_dict()

@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    await cancel_job_tasks(jobs[job_id], review=False)
    return {"ok": True}


@app.post("/api/verify-documents")
async def api_verify_documents(
    request: Request,
    original: UploadFile = File(...),
    translated: UploadFile = File(...),
    source_lang: str = Form("ro"),
    target_lang: str = Form("en"),
):
    """Create a review-only job from two uploaded documents (an original and its
    translation). Segments are aligned by position; the existing review pipeline
    then verifies each (original, translation) pair. No translation is performed.
    """
    validate_languages(source_lang, target_lang)
    original_name = upload_filename(original.filename)
    translated_name = upload_filename(translated.filename)
    allowed = ('.docx', '.pdf', '.txt', '.text', '.md')
    for filename in (original_name, translated_name):
        ext = Path(filename).suffix.lower()
        if ext not in allowed:
            raise HTTPException(400, f"Format nesuportat: {ext}. Acceptate: {', '.join(allowed)}")

    orig_bytes = await original.read(MAX_FILE_SIZE + 1)
    trans_bytes = await translated.read(MAX_FILE_SIZE + 1)
    for b in (orig_bytes, trans_bytes):
        if len(b) > MAX_FILE_SIZE:
            raise HTTPException(400, f"Fișier prea mare (max {MAX_FILE_SIZE // 1024 // 1024}MB)")

    job_id = str(uuid.uuid4())[:8]
    # The translated file is saved under the job-id prefix so a revised document
    # can later be rebuilt on top of it (preserving DOCX formatting).
    orig_path = UPLOAD_DIR / f"{job_id}_orig_{original_name}"
    trans_path = UPLOAD_DIR / f"{job_id}_{translated_name}"
    output_path = OUTPUT_DIR / f"{job_id}_{translated_name}"
    staged_paths = [orig_path, trans_path, output_path,
                    Path(str(orig_path) + '.review.docx'), Path(str(trans_path) + '.review.docx')]

    with file_lease(*staged_paths):
        try:
            orig_path.write_bytes(orig_bytes)
            trans_path.write_bytes(trans_bytes)
            # Include table paragraphs, including merged and nested table cells.
            src_texts, orig_docx = extract_review_segments(orig_path)
            tgt_texts, tgt_docx = extract_review_segments(trans_path)
            if not src_texts or not tgt_texts:
                raise HTTPException(400, "Unul dintre documente nu conține text de verificat.")
            orig_count, trans_count = len(src_texts), len(tgt_texts)
            src_texts, n, mismatch = align_review_segments(src_texts, orig_docx, tgt_texts, tgt_docx)
            if not n:
                raise HTTPException(400, "Documentele nu au segmente corespunzătoare de verificat.")
            output_path.write_bytes(trans_bytes)
        except Exception as e:
            for path in staged_paths:
                path.unlink(missing_ok=True)
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(400, f"Nu am putut citi documentele: {e}")

        # Retain unmatched translated content for reconstruction. run_review skips
        # target segments without an aligned source, but export must not drop them.

        client_ip = request.client.host if request and request.client else "-"
        job = TranslationJob(job_id, translated_name, source_lang, target_lang,
                             model="", ollama_url=DEFAULT_OLLAMA_URL, client_ip=client_ip)
        job.display_filename = translated.filename
        job.kind = "verify"
        job.status = JobStatus.COMPLETED
        job.progress = 100.0
        job.current_step = "Verificare pregătită"
        job.started_at = job.created_at
        job.completed_at = datetime.now().isoformat()
        job.source_segments = src_texts
        job.translated_chunks = tgt_texts
        job.docx_segments = tgt_docx or []
        job.original_filename = original_name
        job.original_docx_segments = orig_docx or []
        if Path(translated_name).suffix.lower() == '.pdf':
            job.original_docx_path = str(Path(str(trans_path) + '.review.docx'))
        job.total_chunks = n
        job.completed_chunks = n
        job.output_file = translated_name
        # Saving the translation to OUTPUT_DIR lets the job's normal download button
        # return the document under review.
        detail = f"original={original.filename}, tradus={translated.filename}, {n} segmente"
        if mismatch:
            detail += f" — numere diferite de segmente ({orig_count} vs {trans_count}), aliniat la {n}"
        job.add_event("info", "Verificare document încărcată", detail)
        jobs[job_id] = job
        emit(job)
        return {"job_id": job_id, "total": n, "original_segments": orig_count,
                "translated_segments": trans_count, "mismatch": mismatch}


class ReviewRequest(BaseModel):
    model: str = ""
    ollama_url: str = ""
    num_ctx: int = DEFAULT_NUM_CTX
    concurrency: int = DEFAULT_CONCURRENCY
    keep_alive: str = DEFAULT_KEEP_ALIVE
    context_window: int = 1  # neighbouring source segments (each side) as context
    context_translations: bool = False  # also include neighbouring translations


@app.post("/api/jobs/{job_id}/review")
async def api_review(job_id: str, req: ReviewRequest):
    """Start an LLM second-pass review of a completed job's translations."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if getattr(job, "deleting", False):
        raise HTTPException(409, "Jobul este în curs de ștergere.")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(400, "Jobul trebuie să fie finalizat înainte de verificare.")
    if not any(job.translated_chunks):
        raise HTTPException(400, "Jobul nu are traduceri de verificat.")
    if job.review_status == "running":
        raise HTTPException(409, "O verificare este deja în curs pentru acest job.")
    model = (req.model or "").strip() or job.model or DEFAULT_MODEL
    url = (req.ollama_url or "").strip() or job.ollama_url or DEFAULT_OLLAMA_URL
    num_ctx = max(512, min(req.num_ctx, 32768))
    concurrency = max(1, min(req.concurrency, 10))
    context_window = max(0, min(req.context_window, 5))
    url = validate_engine_url(url, 'ollama')
    _reset_review(job, model)
    start_review(job, model, url, num_ctx=num_ctx,
                                   concurrency=concurrency, keep_alive=req.keep_alive,
                                   context_window=context_window,
                                   context_translations=bool(req.context_translations),
                                   _prepared=True)
    return {"ok": True, "model": model, "total": len(job.source_segments)}


@app.get("/api/jobs/{job_id}/review")
async def api_review_results(job_id: str):
    """Return the review progress and per-segment results for a job."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    return {
        "job_id": job_id,
        "status": job.review_status,
        "model": job.review_model,
        "progress": round(job.review_progress, 1),
        "total": job.review_total,
        "done": job.review_done,
        "summary": job.review_summary,
        "error": job.review_error,
        "revised_output_file": job.revised_output_file,
        "results": job.review_results,
    }


@app.post("/api/jobs/{job_id}/review/cancel")
async def api_review_cancel(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    await cancel_job_tasks(jobs[job_id], translation=False)
    return {"ok": True}


class ReviewApplyRequest(BaseModel):
    indices: list[int] = []     # segment indices whose suggestion to apply
    apply_all: bool = False     # apply every suggestion that exists
    edits: dict[int, str] = {}  # segment index -> manually edited text (overrides suggestion)


@app.post("/api/jobs/{job_id}/review/apply")
async def api_review_apply(job_id: str, req: ReviewApplyRequest):
    """Regenerate the document, replacing accepted segments with the reviewer's
    improved translation."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if not job.review_results:
        raise HTTPException(400, "Rulează mai întâi verificarea.")
    # Map segment index -> suggestion (only those that actually have one).
    # Prefer the marker-carrying version so inline formatting is reapplied.
    suggestions = {r["index"]: (r.get("suggestion_raw") or r.get("suggestion") or "")
                   for r in job.review_results if (r.get("suggestion") or "").strip()}
    # Manual edits override the suggestion for that segment. Note: manually
    # edited text carries no inline ⟦n⟧ markers, so DOCX inline formatting on
    # an edited segment is not reapplied.
    edits = req.edits or {}
    if req.apply_all:
        chosen = set(suggestions.keys()) | set(edits.keys())
    else:
        chosen = {i for i in req.indices if i in suggestions or i in edits}
    if any(i < 0 or i >= len(job.translated_chunks) for i in chosen):
        raise HTTPException(400, "Indice de segment invalid.")
    if not chosen:
        raise HTTPException(400, "Nicio propunere de aplicat.")

    revised = list(job.translated_chunks)
    for i in chosen:
        if 0 <= i < len(revised):
            revised[i] = edits.get(i, suggestions.get(i, revised[i]))

    try:
        out_path, out_name = build_revised_document(job, revised)
    except Exception as e:
        raise HTTPException(500, f"Nu am putut genera documentul revizuit: {e}")

    job.revised_output_file = out_name
    job.add_event("success", "Document revizuit generat",
                  f"{len(chosen)} propuneri aplicate -> {out_name}")
    emit(job)
    return {"ok": True, "filename": out_name, "applied": len(chosen)}


@app.get("/api/jobs/{job_id}/review/download")
async def api_review_download(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if not job.revised_output_file:
        raise HTTPException(404, "Niciun document revizuit. Aplică mai întâi propunerile.")
    path = OUTPUT_DIR / f"{job_id}_rev_{job.revised_output_file}"
    if not path.exists():
        raise HTTPException(404, "Fișierul revizuit lipsește.")
    return FileResponse(str(path), filename=job.revised_output_file)


@app.get("/api/jobs/{job_id}/review/export-html")
async def api_review_export_html(job_id: str):
    """Export all review suggestions as a standalone HTML file that mirrors
    the graphical interface layout (original, translation, verdict, issues,
    suggestion, score)."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if not job.review_results:
        raise HTTPException(400, "Rulează mai întâi verificarea.")

    html = _build_review_export_html(job)
    filename = f"{Path(job.filename).stem}_verificare_{job.target_lang}.html"
    out_path = OUTPUT_DIR / f"{job.job_id}_export_{filename}"
    out_path.write_text(html, encoding='utf-8')
    job.review_export_file = filename
    return FileResponse(str(out_path), filename=filename, media_type="text/html")


def _build_review_export_html(job: TranslationJob) -> str:
    """Generate a standalone HTML document replicating the review modal UI."""
    import html as html_mod
    results = job.review_results
    summary = job.review_summary or _review_summary(results)
    src_name = LANG_EN.get(job.source_lang, job.source_lang)
    tgt_name = LANG_EN.get(job.target_lang, job.target_lang)

    def esc(text):
        return html_mod.escape(str(text or ""))

    verdict_labels = {'ok': 'OK', 'minor': 'Minor', 'major': 'Major', 'error': 'Eroare', 'unknown': '?'}
    verdict_colors = {'ok': '#16a34a', 'minor': '#d97706', 'major': '#dc2626', 'error': '#94a3b8', 'unknown': '#94a3b8'}
    verdict_bg = {'ok': '#dcfce7', 'minor': '#fef3c7', 'major': '#fee2e2', 'error': '#f1f5f9', 'unknown': '#f1f5f9'}
    border_colors = {'ok': '#16a34a', 'minor': '#d97706', 'major': '#dc2626', 'error': '#94a3b8', 'unknown': '#94a3b8'}

    rows_html = []
    for r in results:
        v = r.get('verdict', 'unknown')
        issues = r.get('issues', [])
        issues_html = ''
        if issues:
            items = ''.join(f'<li>{esc(i)}</li>' for i in issues if i)
            issues_html = f'<div class="issues"><strong>Probleme:</strong><ul>{items}</ul></div>'
        sugg = (r.get('suggestion') or '').strip()
        sugg_html = ''
        if sugg:
            sugg_html = f'<div class="suggestion"><div class="cell-label">Sugestie de îmbunătățire</div><div class="text sugg-text">{esc(sugg)}</div></div>'
        score_html = f'<span class="score">scor {r["score"]}/5</span>' if r.get('score') else ''
        rows_html.append(f'''
        <div class="card" style="border-left-color:{border_colors.get(v, '#94a3b8')}">
          <div class="card-head">
            <span class="badge" style="background:{verdict_bg.get(v, '#f1f5f9')};color:{verdict_colors.get(v, '#94a3b8')}">{verdict_labels.get(v, v)}</span>
            {score_html}
            <span class="seg-num">segment #{r.get("index", 0) + 1}</span>
          </div>
          <div class="row">
            <div><div class="cell-label">Original ({esc(src_name)})</div><div class="text">{esc(r.get("source", ""))}</div></div>
            <div><div class="cell-label">Traducere ({esc(tgt_name)})</div><div class="text">{esc(r.get("translation", ""))}</div></div>
          </div>
          {issues_html}
          {sugg_html}
        </div>''')

    summary_html = ''
    if summary:
        summary_html = f'''
        <div class="summary">
          <span class="badge" style="background:#dcfce7;color:#16a34a">{summary.get('ok', 0)} OK</span>
          <span class="badge" style="background:#fef3c7;color:#d97706">{summary.get('minor', 0)} minore</span>
          <span class="badge" style="background:#fee2e2;color:#dc2626">{summary.get('major', 0)} majore</span>
          {('<span class="badge" style="background:#f1f5f9;color:#94a3b8">' + str(summary.get('errors', 0)) + ' erori</span>') if summary.get('errors') else ''}
          {('<span class="badge" style="background:#dbeafe;color:#2563eb">Scor mediu ' + str(summary.get('avg_score', '')) + '/5</span>') if summary.get('avg_score') else ''}
        </div>'''

    return f'''<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verificare traducere — {esc(job.filename)}</title>
<style>
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f8fafc;color:#0f172a;padding:24px;max-width:960px;margin:0 auto}}
h1{{font-size:20px;font-weight:700;color:#2563eb;margin-bottom:4px}}
.meta{{font-size:13px;color:#475569;margin-bottom:16px}}
.summary{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}}
.badge{{display:inline-flex;align-items:center;padding:4px 12px;border-radius:12px;font-size:12px;font-weight:600}}
.card{{border:1px solid #e2e8f0;border-left:4px solid #94a3b8;border-radius:10px;padding:14px;margin-bottom:12px;background:#fff}}
.card-head{{display:flex;align-items:center;gap:8px;margin-bottom:8px}}
.seg-num{{font-size:11px;color:#94a3b8;margin-left:auto}}
.score{{font-size:12px;color:#94a3b8}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:720px){{.row{{grid-template-columns:1fr}}}}
.cell-label{{font-size:10px;text-transform:uppercase;font-weight:700;color:#94a3b8;margin-bottom:3px}}
.text{{font-size:13px;background:#f8fafc;border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-word;border:1px solid #e2e8f0}}
.sugg-text{{border:1px dashed #2563eb;background:#eff6ff}}
.issues{{margin-top:8px;font-size:12px;color:#475569}}
.issues ul{{margin-left:18px}}
.issues li{{margin-bottom:2px}}
.suggestion{{margin-top:10px}}
.footer{{margin-top:24px;padding-top:16px;border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8;text-align:center}}
</style>
</head>
<body>
<h1>\U0001f50d Verificare traducere</h1>
<div class="meta">
  <strong>Fișier:</strong> {esc(job.filename)} &nbsp;|&nbsp;
  <strong>Limbi:</strong> {esc(src_name)} → {esc(tgt_name)} &nbsp;|&nbsp;
  <strong>Model:</strong> {esc(job.review_model or '-')} &nbsp;|&nbsp;
  <strong>Segmente:</strong> {len(results)}
</div>
{summary_html}
{''.join(rows_html)}
<div class="footer">Generat de Traducător Offline · {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
</body>
</html>'''


class ReviewTextRequest(BaseModel):
    source_text: str
    translated_text: str
    source_lang: str = "ro"
    target_lang: str = "en"
    model: str = ""
    ollama_url: str = ""
    num_ctx: int = DEFAULT_NUM_CTX


@app.post("/api/review-text")
async def api_review_text(req: ReviewTextRequest):
    """Ad-hoc review of a single source/translation pair (used by the text page)."""
    src_text = (req.source_text or "").strip()
    tgt_text = (req.translated_text or "").strip()
    if not src_text or not tgt_text:
        raise HTTPException(400, "Sursa și traducerea sunt obligatorii.")
    if len(src_text) > 50000 or len(tgt_text) > 50000:
        raise HTTPException(400, "Text prea lung (maximum 50.000 de caractere per câmp).")
    validate_languages(req.source_lang, req.target_lang)
    model = (req.model or "").strip() or DEFAULT_MODEL
    url = validate_engine_url(req.ollama_url)
    num_ctx = max(512, min(req.num_ctx, 32768))
    try:
        async with aiohttp.ClientSession() as session:
            parsed = await review_segment(session, src_text, tgt_text,
                                          req.source_lang, req.target_lang,
                                          model, url, num_ctx=num_ctx, job_id="text")
        return {"ok": True, "model": model, "review": parsed}
    except Exception as e:
        raise HTTPException(500, f"Verificare eșuată: {e}")

@app.get("/api/jobs/{job_id}/download")
async def api_download(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    job = jobs[job_id]
    if not job.output_file:
        raise HTTPException(400, "Not ready")
    path = OUTPUT_DIR / f"{job_id}_{job.output_file}"
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(str(path), filename=job.output_file)

@app.delete("/api/jobs/{job_id}")
async def api_delete(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    job = jobs[job_id]
    if getattr(job, "deleting", False):
        raise HTTPException(409, "Jobul este în curs de ștergere.")
    # Reserve deletion before awaiting cancellation so another request cannot
    # start a review in the gap between stopping work and removing its files.
    job.deleting = True
    await cancel_job_tasks(job)
    jobs.pop(job_id, None)
    # Includes converted PDF structure, previous review exports and partial files.
    for directory in (UPLOAD_DIR, OUTPUT_DIR):
        for path in directory.iterdir():
            if path.is_file() and path.name.startswith(job_id + "_"):
                path.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/jobs/stop-all")
async def api_stop_all():
    active = [job for job in list(jobs.values())
              if job.status in (JobStatus.QUEUED, JobStatus.EXTRACTING,
                 JobStatus.CHUNKING, JobStatus.TRANSLATING, JobStatus.ASSEMBLING)
              or job.review_status == "running"]
    await asyncio.gather(*(cancel_job_tasks(job) for job in active))
    app_logger.warning(f"Stop all: {len(active)} jobs cancelled")
    return {"ok": True, "stopped": len(active)}

# ============================================================
# Logs & Files Endpoints
# ============================================================

@app.get("/api/logs")
async def api_logs(limit: int = 500, level: str = ""):
    logs = list(log_handler.logs)
    if level:
        logs = [l for l in logs if l["level"] == level.upper()]
    return {"logs": logs[-limit:], "total": len(log_handler.logs)}

@app.get("/api/prompts")
async def api_prompts(limit: int = 200, job_id: str = "", engine: str = ""):
    """Return the prompt/response history (what was sent to / received from the
    translation engine). Optionally filter by job_id or engine."""
    items = list(prompt_log)
    if job_id:
        items = [p for p in items if p["job_id"] == job_id]
    if engine:
        items = [p for p in items if p["engine"] == engine]
    return {"prompts": items[-limit:][::-1], "total": len(prompt_log)}

@app.delete("/api/prompts")
async def api_clear_prompts():
    """Clear the prompt/response history."""
    n = len(prompt_log)
    prompt_log.clear()
    app_logger.info(f"Prompt history cleared ({n} entries)")
    return {"ok": True, "cleared": n}

@app.get("/api/files")
async def api_files():
    files = []
    for d, category in [(UPLOAD_DIR, "upload"), (OUTPUT_DIR, "output")]:
        if d.exists():
            for f in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file() and not f.is_symlink():
                    stat = f.stat()
                    files.append({
                        "name": f.name,
                        "category": category,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "path": str(f.relative_to(BASE_DIR))
                    })
    return {"files": files, "upload_dir": str(UPLOAD_DIR), "output_dir": str(OUTPUT_DIR)}

def stored_file(category: str, filename: str):
    if not filename or filename in (".", "..") or any(c in filename for c in ("/", "\\", "\0")):
        raise HTTPException(400, "Nume de fișier invalid.")
    if category == "upload":
        path = UPLOAD_DIR / filename
    elif category == "output":
        path = OUTPUT_DIR / filename
    else:
        raise HTTPException(400, "Invalid category")
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, "File not found")
    return path


@app.get("/api/files/{category}/{filename}")
async def api_download_file(category: str, filename: str):
    path = stored_file(category, filename)
    if is_file_active(path):
        raise HTTPException(409, "Fișierul este utilizat de o operație activă.")
    return FileResponse(str(path), filename=filename)


@app.delete("/api/files/{category}/{filename}")
async def api_delete_file(category: str, filename: str):
    path = stored_file(category, filename)
    if is_file_active(path):
        raise HTTPException(409, "Fișierul este utilizat de o operație activă.")
    path.unlink()
    app_logger.info(f"File deleted: {category}/{filename}")
    return {"ok": True}

@app.post("/api/files/cleanup")
async def api_cleanup_files():
    deleted, skipped = _cleanup_files(older_than=None)
    app_logger.warning(f"Cleanup: {deleted} files deleted; {skipped} active files retained")
    return {"ok": True, "deleted": deleted, "skipped_active": skipped}

# ============================================================
# OCR Standalone Endpoint
# ============================================================

TESS_LANG_MAP = {
    'ro': 'ron', 'en': 'eng', 'fr': 'fra', 'de': 'deu',
    'es': 'spa', 'it': 'ita', 'pt': 'por', 'nl': 'nld',
    'pl': 'pol', 'ru': 'rus', 'uk': 'ukr', 'bg': 'bul',
    'hr': 'hrv', 'cs': 'ces', 'sk': 'slk', 'hu': 'hun',
    'el': 'ell', 'tr': 'tur', 'ar': 'ara', 'ja': 'jpn',
    'ko': 'kor', 'zh-Hans': 'chi_sim', 'zh-Hant': 'chi_tra'
}

@app.post("/api/ocr")
async def api_ocr(file: UploadFile = File(...), lang: str = Form("ro")):
    """Extract digital/OCR text, including all DOCX table/header/footer stories."""
    ext = Path(file.filename or '').suffix.lower()
    allowed = ('.pdf', '.docx', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp')
    if ext not in allowed:
        raise HTTPException(400, f"Format nesuportat: {ext}. Acceptate: {', '.join(allowed)}")
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"Fișier prea mare (max {MAX_FILE_SIZE // 1024 // 1024}MB)")
    ocr_id = str(uuid.uuid4())[:8]
    input_path = UPLOAD_DIR / f'ocr_{ocr_id}{ext}'
    out_docx_path = OUTPUT_DIR / f'ocr_{ocr_id}_result.docx'
    out_pdf_path = OUTPUT_DIR / f'ocr_{ocr_id}_result.pdf'
    tess_lang = TESS_LANG_MAP.get(lang, 'eng')
    if tess_lang != 'eng':
        tess_lang += '+eng'
    with file_lease(input_path, out_docx_path, out_pdf_path):
        try:
            input_path.write_bytes(content)
            if ext == '.docx':
                segments = extract_docx_segments(input_path, tag_inline=False)
                extracted_text = '\n\n'.join(seg['text'] for seg in segments)
            elif ext == '.pdf':
                pages = []
                with pdfplumber.open(str(input_path)) as pdf:
                    for index, page in enumerate(pdf.pages):
                        text = (_ocr_pdf_page(input_path, index + 1, tess_lang)
                                if _page_requires_ocr(page) else page.extract_text() or '')
                        if text.strip():
                            pages.append(f'--- Pagina {index + 1} ---\n\n{text.strip()}')
                extracted_text = '\n\n'.join(pages)
            else:
                if not TESSERACT_AVAILABLE:
                    raise HTTPException(503, 'Tesseract OCR nu este disponibil pe server.')
                with Image.open(str(input_path)) as img:
                    extracted_text = pytesseract.image_to_string(img, lang=tess_lang).strip()
            if not extracted_text.strip():
                raise HTTPException(400, 'Nu a fost identificat text în document.')
            result = docx.Document()
            result.add_heading(f'OCR: {file.filename}', level=1)
            for paragraph in extracted_text.split('\n\n'):
                if paragraph.strip():
                    result.add_paragraph(paragraph.strip())
            result.save(str(out_docx_path))
            pdf_ready = False
            try:
                converted = convert_docx_to_pdf_libreoffice(out_docx_path, OUTPUT_DIR)
                if converted != out_pdf_path:
                    converted.rename(out_pdf_path)
                pdf_ready = out_pdf_path.exists()
            except Exception as exc:
                app_logger.warning(f'OCR PDF conversion failed: {exc}, falling back to DOCX')
            return {'ocr_id': ocr_id, 'filename': file.filename,
                    'text': extracted_text, 'char_count': len(extracted_text),
                    'download_url': f'/api/ocr/{ocr_id}/download',
                    'download_name': Path(file.filename or 'document').stem + ('_ocr.pdf' if pdf_ready else '_ocr.docx')}
        except HTTPException:
            out_docx_path.unlink(missing_ok=True)
            out_pdf_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            out_docx_path.unlink(missing_ok=True)
            out_pdf_path.unlink(missing_ok=True)
            raise HTTPException(500, f'Eroare OCR: {exc}') from exc
        finally:
            input_path.unlink(missing_ok=True)

@app.get("/api/ocr/{ocr_id}/download")
async def api_ocr_download(ocr_id: str):
    """Download OCR result file."""
    # Try PDF first, then DOCX
    pdf_path = OUTPUT_DIR / f"ocr_{ocr_id}_result.pdf"
    docx_path = OUTPUT_DIR / f"ocr_{ocr_id}_result.docx"
    if pdf_path.exists():
        return FileResponse(str(pdf_path), filename=f"ocr_result.pdf",
                            media_type="application/pdf")
    elif docx_path.exists():
        return FileResponse(str(docx_path), filename=f"ocr_result.docx",
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    raise HTTPException(404, "Fișierul OCR nu a fost găsit.")

@app.get("/api/ocr/status")
async def api_ocr_status():
    """Check OCR availability."""
    return {
        "tesseract_available": TESSERACT_AVAILABLE,
        "libreoffice": check_libreoffice_available()
    }

@app.get("/api/libreoffice")
async def api_libreoffice():
    return check_libreoffice_available()

@app.get("/api/system-info")
async def api_system_info():
    import platform
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": psutil.cpu_count(),
        "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
        "server_start": server_start_time.isoformat(),
        "uptime_seconds": round((datetime.now() - server_start_time).total_seconds()),
        "upload_dir": str(UPLOAD_DIR),
        "output_dir": str(OUTPUT_DIR),
        "total_jobs": len(jobs),
        "active_jobs": sum(1 for j in jobs.values() if j.status in
                          (JobStatus.QUEUED, JobStatus.EXTRACTING, JobStatus.CHUNKING,
                           JobStatus.TRANSLATING, JobStatus.ASSEMBLING)),
        "ollama_url": DEFAULT_OLLAMA_URL,
        "libretranslate_url": DEFAULT_LIBRETRANSLATE_URL,
        "installed_languages": _get_installed_languages()
    }

@app.get("/api/network-diag")
async def api_network_diagnostics():
    """Run network diagnostics to help troubleshoot Ollama connectivity."""
    import subprocess as sp
    results = {"tests": [], "ollama_url": DEFAULT_OLLAMA_URL}

    # Get container network info
    try:
        gw = sp.run(['ip', 'route'], capture_output=True, text=True, timeout=5)
        results["routes"] = gw.stdout.strip()
    except:
        results["routes"] = "unavailable"

    try:
        hostname_out = sp.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
        results["container_ip"] = hostname_out.stdout.strip()
    except:
        results["container_ip"] = "unavailable"

    # Test various host endpoints
    ollama_port = os.environ.get("OLLAMA_PORT", "11434")
    test_hosts = [
        "host.containers.internal",
        "host.docker.internal",
        "host.wsl.internal",
    ]
    # Add gateway IP
    try:
        gw_line = sp.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True, timeout=5)
        gw_ip = gw_line.stdout.strip().split()[2] if gw_line.stdout.strip() else None
        if gw_ip:
            test_hosts.append(gw_ip)
            results["gateway_ip"] = gw_ip
    except:
        pass

    for host in test_hosts:
        test = {"host": host, "ping": False, "ollama": False, "resolved_ip": None}
        # Ping test
        try:
            ping = sp.run(['ping', '-c', '1', '-W', '2', host],
                          capture_output=True, text=True, timeout=5)
            test["ping"] = (ping.returncode == 0)
        except:
            pass
        # DNS resolution
        try:
            getent = sp.run(['getent', 'hosts', host],
                            capture_output=True, text=True, timeout=5)
            if getent.returncode == 0:
                test["resolved_ip"] = getent.stdout.strip().split()[0]
        except:
            pass
        # Ollama API test
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{host}:{ollama_port}/api/version", allow_redirects=False,
                                       timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        test["ollama"] = True
                        data = await resp.json()
                        test["ollama_version"] = data.get("version", "unknown")
        except:
            pass
        results["tests"].append(test)

    return results

@app.get("/api/jobs/{job_id}/events")
async def api_sse(request: Request, job_id: str):
    if job_id not in jobs:
        raise HTTPException(404)
    q = asyncio.Queue(maxsize=200)
    sse_queues.setdefault(job_id, []).append(q)
    async def gen():
        try:
            yield f"data: {json.dumps(jobs[job_id].to_dict())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(data)}\n\n"
                    if data.get("status") in ("completed", "failed", "cancelled"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if job_id in sse_queues and q in sse_queues[job_id]:
                sse_queues[job_id].remove(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ============================================================
# Scheduled Cleanup (daily at midnight)
# ============================================================

def _cleanup_files(older_than=None):
    deleted = skipped = 0
    for directory in (UPLOAD_DIR, OUTPUT_DIR):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            if is_file_active(path):
                skipped += 1
                continue
            try:
                if older_than is not None and path.stat().st_mtime > older_than:
                    continue
                path.unlink(missing_ok=True)
                deleted += 1
            except OSError as exc:
                app_logger.error(f"Cleanup failed for {path.name}: {exc}")
    return deleted, skipped


def cleanup_old_files():
    """Expire old inactive files, never source files needed by current work."""
    hours = max(1.0, float(os.environ.get("FILE_RETENTION_HOURS", "24")))
    deleted, skipped = _cleanup_files(older_than=time.time() - hours * 3600)
    app_logger.info(f"Scheduled cleanup: {deleted} expired files deleted; {skipped} active files retained")
    return deleted


_cleanup_task = None


def schedule_daily_cleanup():
    """Schedule retention on the API loop so cleanup cannot race job dispatch."""
    global _cleanup_task
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Direct script startup reaches the ASGI startup hook after this call.
        return
    if _cleanup_task is not None and not _cleanup_task.done():
        return
    async def run_cleanup():
        while True:
            now = datetime.now()
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            await asyncio.sleep((midnight - now).total_seconds())
            cleanup_old_files()
    _cleanup_task = asyncio.create_task(run_cleanup())
    app_logger.info("Daily retention cleanup started (inactive files only)")

# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    schedule_daily_cleanup()
    print("=" * 60)
    print("  Offline Document Translator - Server")
    print(f"  Web UI:       http://0.0.0.0:{port}")
    print(f"  Ollama:       {DEFAULT_OLLAMA_URL} (external)")
    print(f"  LibreTranslate: {DEFAULT_LIBRETRANSLATE_URL} (internal)")
    print(f"  Cleanup:      daily at midnight")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
