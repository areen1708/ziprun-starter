from __future__ import annotations

import json
import shlex
import shutil
import tempfile
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="ZipRun API", version="0.2.0")

# Local development only. Do not expose this API publicly as-is.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_FILES = 2000
MAX_RUNTIME_SECONDS = 12
MAX_OUTPUT_BYTES = 24_000
MAX_PACKAGE_JSON_BYTES = 512 * 1024
MAX_SCAN_DEPTH = 4
MAX_SCAN_DIRS = 400

RUNTIME_IMAGES: dict[str, tuple[str, ...]] = {
    "python": ("python:3.12-alpine", "python:3.12-slim"),
    "node": ("node:22-alpine",),
    "cpp": ("gcc:14",),
    "java": ("eclipse-temurin:21-jdk",),
}

LANGUAGE_NAMES: dict[str, str] = {
    "python": "Python",
    "cpp": "C++",
    "java": "Java",
    "node": "Node.js",
}

SINGLE_FILE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".java": "java",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
}

CANONICAL_ENTRIES = ("main.py", "main.cpp", "Main.java", "index.js", "server.js", "app.js")

JUNK_NAMES = frozenset({"__MACOSX", ".DS_Store", ".git", ".svn", ".idea", ".vscode", "node_modules", "__pycache__", ".venv", "venv"})

DOCKERFILE_MESSAGE = (
    "Dockerfile/Compose projects are not executed by ZipRun. "
    "Running uploaded build definitions needs a separate hardened build pipeline."
)

NO_ENTRY_MESSAGE = (
    "Could not identify a supported entry point. Supported entry points are main.py, main.cpp, "
    "Main.java, index.js, server.js, app.js, a package.json start script without dependencies, "
    "or a single Python/C++/Java/JavaScript source file."
)


@dataclass(frozen=True)
class ExecutionPlan:
    language: str
    kind: str
    workdir: str
    entry: str
    command: str


def is_junk(name: str) -> bool:
    return name in JUNK_NAMES or name.startswith("._")


def is_safe_segment(name: str) -> bool:
    if not name or len(name) > 128 or name in {".", ".."}:
        return False
    if name[0] in ".-" or "/" in name or "\\" in name:
        return False
    return name.isprintable()


def safe_extract(zf: zipfile.ZipFile, destination: Path) -> None:
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise ValueError(f"Archive contains too many entries (limit {MAX_FILES}).")

    declared_total = sum(info.file_size for info in infos)
    if declared_total > MAX_EXTRACTED_BYTES:
        raise ValueError("Uncompressed archive exceeds the 100 MB limit.")

    written = 0
    for info in infos:
        raw_name = info.filename
        path = PurePosixPath(raw_name)

        if not raw_name or path.is_absolute() or ".." in path.parts:
            raise ValueError("Archive contains an unsafe file path.")
        if "\x00" in raw_name or "\\" in raw_name:
            raise ValueError("Archive contains an invalid file path.")

        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if (unix_mode & 0o170000) == 0o120000:
            raise ValueError("Archive contains a symbolic link, which is not allowed.")

        target = destination.joinpath(*path.parts)
        resolved_destination = destination.resolve()
        resolved_target = target.resolve()
        if resolved_target != resolved_destination and resolved_destination not in resolved_target.parents:
            raise ValueError("Archive attempts to write outside its extraction directory.")

        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info, "r") as source, target.open("wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_EXTRACTED_BYTES:
                    raise ValueError("Uncompressed archive exceeds the 100 MB limit.")
                output.write(chunk)


def locate_project_root(extraction: Path) -> Path:
    current = extraction
    while True:
        try:
            entries = [item for item in sorted(current.iterdir(), key=lambda item: item.name) if not is_junk(item.name)]
        except OSError:
            return current
        if len(entries) == 1 and entries[0].is_dir():
            current = entries[0]
            continue
        return current


def scan_project_dirs(root: Path) -> list[tuple[str, Path]]:
    scanned: list[tuple[str, Path]] = [("", root)]
    queue: deque[tuple[str, Path]] = deque([("", root)])
    while queue:
        rel, path = queue.popleft()
        if rel.count("/") >= MAX_SCAN_DEPTH:
            continue
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or is_junk(child.name):
                continue
            child_rel = f"{rel}/{child.name}" if rel else child.name
            scanned.append((child_rel, child))
            if len(scanned) >= MAX_SCAN_DIRS:
                return scanned
            queue.append((child_rel, child))
    return scanned


def build_command(workdir: str, body: str) -> str:
    cwd = "/tmp/project" if workdir in ("", ".") else f"/tmp/project/{workdir}"
    return f"mkdir -p /tmp/project && cp -R /input/. /tmp/project/ && cd {shlex.quote(cwd)} && {body}"


def program_body(kind: str, name: str) -> str:
    quoted = shlex.quote(name)
    if kind == "python":
        return f"python {quoted}"
    if kind == "cpp":
        return f"g++ -std=c++17 -O2 -pipe {quoted} -o program && ./program"
    if kind == "java":
        return f"javac {quoted} && java {shlex.quote(Path(name).stem)}"
    return f"node {quoted}"


def build_plan(name: str, workdir: str) -> ExecutionPlan:
    kind = SINGLE_FILE_EXTENSIONS[Path(name).suffix.lower()]
    return ExecutionPlan(
        language=LANGUAGE_NAMES[kind],
        kind=kind,
        workdir=workdir,
        entry=name,
        command=build_command(workdir, program_body(kind, name)),
    )


def plan_for_entry(entry: str, dirs: list[tuple[str, Path]]) -> ExecutionPlan | None:
    matches = [(rel, path) for rel, path in dirs if (path / entry).is_file()]
    if not matches:
        return None
    if len(matches) > 1:
        locations = ", ".join(rel or "." for rel, _ in matches)
        raise ValueError(f"Multiple {entry} files found ({locations}). Keep exactly one so ZipRun knows what to run.")
    return build_plan(entry, matches[0][0] or ".")


def plan_from_package(package_path: Path, workdir: str) -> ExecutionPlan | None:
    if package_path.stat().st_size > MAX_PACKAGE_JSON_BYTES:
        raise ValueError("package.json is too large (limit 512 KB).")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("package.json is not valid JSON.")
    if not isinstance(package, dict):
        raise ValueError("package.json must contain a JSON object.")

    dependencies = package.get("dependencies") or {}
    dev_dependencies = package.get("devDependencies") or {}
    if dependencies or dev_dependencies:
        raise ValueError(
            "This project declares npm dependencies. Dependency installation is not enabled in the MVP."
        )

    scripts = package.get("scripts") or {}
    if isinstance(scripts, dict) and isinstance(scripts.get("start"), str):
        return ExecutionPlan(
            language=LANGUAGE_NAMES["node"],
            kind="node",
            workdir=workdir,
            entry="package.json",
            command=build_command(workdir, "npm start"),
        )
    return None


def plan_for_single_source_file(dirs: list[tuple[str, Path]]) -> ExecutionPlan:
    candidates: list[tuple[str, str]] = []
    for rel, directory in dirs:
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            if not child.is_file() or is_junk(child.name):
                continue
            if child.name in CANONICAL_ENTRIES:
                continue
            if child.suffix.lower() not in SINGLE_FILE_EXTENSIONS:
                continue
            if not is_safe_segment(child.name):
                continue
            candidates.append((rel or ".", child.name))

    if len(candidates) == 1:
        workdir, name = candidates[0]
        return build_plan(name, workdir)
    if len(candidates) > 1:
        names = ", ".join(sorted({name for _, name in candidates}))[:200]
        raise ValueError(
            f"No canonical entry point found and the archive contains multiple source files ({names}). "
            "Add a main.py, main.cpp, Main.java or index.js so ZipRun knows what to run."
        )
    raise ValueError(NO_ENTRY_MESSAGE)


def detect_plan(project_root: Path) -> ExecutionPlan:
    if any((project_root / marker).exists() for marker in ("Dockerfile", "docker-compose.yml", "compose.yaml")):
        raise ValueError(DOCKERFILE_MESSAGE)

    dirs = scan_project_dirs(project_root)

    for entry in ("main.py", "main.cpp", "Main.java"):
        plan = plan_for_entry(entry, dirs)
        if plan is not None:
            return plan

    for rel, directory in dirs:
        package = directory / "package.json"
        if not package.is_file():
            continue
        plan = plan_from_package(package, rel or ".")
        if plan is not None:
            return plan
        break

    for entry in ("index.js", "server.js", "app.js"):
        plan = plan_for_entry(entry, dirs)
        if plan is not None:
            return plan

    return plan_for_single_source_file(dirs)


def detect_project(project: Path) -> tuple[str, str, str]:
    plan = detect_plan(project)
    return (plan.language, RUNTIME_IMAGES[plan.kind][0], plan.command)


def resolve_image(kind: str, client: Any) -> str:
    for image_name in RUNTIME_IMAGES[kind]:
        try:
            client.images.get(image_name)
            return image_name
        except (ImageNotFound, DockerException):
            continue
    target = RUNTIME_IMAGES[kind][0]
    try:
        client.images.pull(target)
    except DockerException as exc:
        raise RuntimeError(f"Could not download runtime image {target}. Check Docker and internet access.") from exc
    return target


def decode_output(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def cap_output(text: str) -> str:
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return text[-MAX_OUTPUT_BYTES:] + "\n\n[Output truncated by ZipRun.]"
    return text


def run_in_sandbox(project: Path, plan: ExecutionPlan) -> dict[str, Any]:
    try:
        client = docker.from_env()
        client.ping()
    except DockerException as exc:
        raise RuntimeError("Docker is unavailable. Start Docker Desktop and retry.") from exc

    container = None
    started = time.monotonic()
    timed_out = False
    try:
        image = resolve_image(plan.kind, client)
        container = client.containers.create(
            image=image,
            command=["/bin/sh", "-lc", plan.command],
            working_dir="/tmp",
            user="1000:1000",
            environment={"HOME": "/tmp", "npm_config_cache": "/tmp/npm-cache"},
            volumes={str(project.resolve()): {"bind": "/input", "mode": "ro"}},
            network_disabled=True,
            read_only=True,
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            pids_limit=128,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            tmpfs={"/tmp": "rw,exec,nosuid,nodev,size=256m,mode=1777"},
            log_config=docker.types.LogConfig(
                type="local", config={"max-size": "1m", "max-file": "2", "compress": "false"}
            ),
            labels={"app": "ziprun", "purpose": "untrusted-code-runner"},
        )
        container.start()

        deadline = started + MAX_RUNTIME_SECONDS
        while time.monotonic() < deadline:
            container.reload()
            if container.status in ("exited", "dead"):
                break
            time.sleep(0.2)
        else:
            timed_out = True
            try:
                container.kill()
            except DockerException:
                pass

        try:
            container.wait(timeout=5)
        except Exception:
            pass

        container.reload()
        exit_code = container.attrs.get("State", {}).get("ExitCode")
        stdout_text = cap_output(decode_output(container.logs(stdout=True, stderr=False, tail=1000)))
        stderr_text = cap_output(decode_output(container.logs(stdout=False, stderr=True, tail=1000)))
        status = "timeout" if timed_out else ("success" if exit_code == 0 else "error")
        return {
            "language": plan.language,
            "image": image,
            "entry": plan.entry,
            "workdir": plan.workdir,
            "command": plan.command,
            "output": cap_output(stdout_text + stderr_text),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_seconds": round(time.monotonic() - started, 2),
            "status": status,
        }
    except DockerException as exc:
        raise RuntimeError(f"Docker execution failed: {exc.__class__.__name__}: {exc}") from exc
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except DockerException:
                pass
        try:
            client.close()
        except Exception:
            pass


def result_response(result: dict[str, Any]) -> JSONResponse:
    status = result.get("status")
    if status == "success":
        return JSONResponse(status_code=200, content=result)
    if status == "timeout":
        detail = f"Execution stopped after the {MAX_RUNTIME_SECONDS} second time limit."
        return JSONResponse(status_code=408, content={**result, "detail": detail})
    detail = f"The program failed with exit code {result.get('exit_code')}."
    return JSONResponse(status_code=500, content={**result, "detail": detail})


async def stream_upload(upload: UploadFile, destination: Path) -> int:
    total = 0
    with destination.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Upload exceeds the 20 MB limit.")
            handle.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return total


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "ZipRun API",
        "version": app.version,
        "status": "running",
        "ui": "http://127.0.0.1:5173",
        "endpoints": {"health": "/api/health", "run": "POST /api/run"},
        "docs": "/docs",
        "note": "This is the backend API. Open the web UI at http://127.0.0.1:5173",
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    try:
        client = docker.from_env()
        client.ping()
        client.close()
        return {"status": "ok", "docker": "connected"}
    except DockerException:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "docker": "unavailable", "detail": "Start Docker Desktop."},
        )


@app.post("/api/run")
async def upload_and_run(file: UploadFile = File(...)) -> JSONResponse:
    filename = (file.filename or "").strip()
    lowered = filename.lower()

    if not filename:
        raise HTTPException(status_code=400, detail="Upload a .zip archive or a source file.")

    is_archive = lowered.endswith(".zip")
    is_source = any(lowered.endswith(ext) for ext in SINGLE_FILE_EXTENSIONS)
    if not is_archive and not is_source:
        raise HTTPException(
            status_code=400,
            detail="Upload a .zip archive or a supported source file (.py, .cpp, .cc, .cxx, .java, .js, .mjs, .cjs).",
        )

    with tempfile.TemporaryDirectory(prefix="ziprun-") as temp:
        temp_path = Path(temp)
        extraction = temp_path / "extracted"
        extraction.mkdir()

        try:
            if is_archive:
                archive_path = temp_path / "upload.zip"
                await stream_upload(file, archive_path)
                try:
                    with zipfile.ZipFile(archive_path) as zf:
                        safe_extract(zf, extraction)
                except zipfile.BadZipFile:
                    raise HTTPException(status_code=400, detail="The uploaded file is not a valid ZIP archive.")
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
                project = locate_project_root(extraction)
            else:
                name = PurePosixPath(filename.replace("\\", "/")).name
                if not is_safe_segment(name):
                    raise HTTPException(status_code=400, detail="The uploaded file name is not supported.")
                target = extraction / name
                await stream_upload(file, target)
                project = extraction

            try:
                plan = detect_plan(project)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

            try:
                result = run_in_sandbox(project, plan)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc))

            result["filename"] = filename
            return result_response(result)
        finally:
            await file.close()
