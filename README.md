# ZipRun — secure project runner MVP

ZipRun is a local web app that accepts a ZIP archive, detects a supported project entry point, runs it inside a restricted Docker container, and displays stdout/stderr.

## MVP scope

Uploads can be either a `.zip` archive or a single source file (`.py`, `.cpp`, `.cc`, `.cxx`, `.java`, `.js`, `.mjs`, `.cjs`).

Supported entry points:
- Python: `main.py`, or a single `.py` file
- C++: `main.cpp`, or a single `.cpp`/`.cc`/`.cxx` file
- Java: `Main.java`, or a single `.java` file
- Node.js: `index.js`, `server.js`, `app.js`, or `package.json` with a `start` script and **no dependencies**

Entry points are detected at the project root, inside a single enclosing project directory (GitHub-style ZIPs), or in a nested directory of the archive.

This MVP deliberately does **not** run arbitrary `Dockerfile` / `docker-compose.yml`, install arbitrary dependencies, or expose web servers from uploaded projects. It displays process output only. Multi-service projects and browser previews are a later phase.

## API

`POST /api/run` accepts `multipart/form-data` with a `file` field.

| Status | Meaning |
| --- | --- |
| 200 | The program ran and exited with code 0 |
| 400 | Invalid archive, empty upload, unsupported file type, or unsafe archive content |
| 408 | The program exceeded the 12 second runtime limit |
| 413 | Upload larger than 20 MB |
| 422 | No supported entry point, ambiguous entry points, or a Dockerfile/Compose project |
| 500 | The program failed (non-zero exit code, including compilation errors) |
| 503 | Docker is unavailable or the runtime image could not be downloaded |

Execution results (including 408 and 500) include `status`, `output`, `stdout`, `stderr`, `exit_code`, `timed_out`, `duration_seconds`, `language`, `image` and `entry`.

## Requirements

- macOS with Docker Desktop installed and running
- Python 3.11+
- Node.js 20+ and npm
- VS Code (recommended)

## Quick start

From the project root, with Docker Desktop running:

```bash
./start.sh
```

This script creates `backend/.venv`, installs backend and frontend dependencies, stops anything already on ports 8001/5173, starts both servers, and waits until they respond. Then open **http://127.0.0.1:5173**.

Use `start.sh` instead of running `uvicorn` directly. A bare `uvicorn` uses your system Python, which does not have the `docker` package installed and fails with `ModuleNotFoundError: No module named 'docker'`.

To stop the servers:

```bash
kill -9 $(lsof -nP -iTCP:8001 -sTCP:LISTEN -t) $(lsof -nP -iTCP:5173 -sTCP:LISTEN -t)
```

## Manual start

## 1. Start Docker Desktop

Install Docker Desktop for Apple silicon from https://www.docker.com/products/docker-desktop/ and open it. Wait until Docker reports that it is running.

Verify:

```bash
docker version
docker run --rm hello-world
```

## 2. Start the backend

Open Terminal in this directory:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
uvicorn main:app --reload --host 127.0.0.1 --port 8001
```

The first time you run each language, Docker may download its official runtime image.

## 3. Start the frontend

In a second Terminal window:

```bash
cd frontend
npm install
npm run dev
```

Open the local URL printed by Vite, usually http://localhost:5173.

## 4. Try the sample projects

Upload either a single source file or a ZIP archive containing one of these entry points:
- `main.py` (or any single `.py` file)
- `main.cpp`
- `Main.java`
- `index.js`

For Node.js, a `package.json` may contain a `start` script only if the project does not need external dependencies.

## 5. Run the backend tests

```bash
cd backend
source .venv/bin/activate
python -m pytest -v
```

The suite covers upload validation, entry-point detection (including nested archives), and real Docker execution for Python, C++, Java and Node.js. Docker execution tests are skipped automatically when the Docker daemon is not running.

## Security model and limitations

- Uploaded ZIP paths are validated; symlinks, path traversal, excessive file counts, oversized archives, and shell-unsafe names are rejected.
- Commands sent to the container are built only from a fixed template; uploaded code, file names, and `package.json` scripts are never executed as shell commands on the host.
- The uploaded project is mounted read-only into the container and copied to the container's temporary filesystem.
- Containers have no network, no Linux capabilities, no privilege escalation, a read-only root filesystem, a PID limit, and CPU/memory limits.
- Uploaded code is still untrusted. This is a local development MVP, not a hardened public multi-user service. Do not expose the backend to the internet.
- Do not add a Docker socket mount to runner containers.
- Do not execute uploaded Dockerfiles or Compose files.
- Dependencies are intentionally not installed yet. Package managers can execute arbitrary install scripts and may require network access.
- The MVP captures terminal output; it does not yet preview web applications in a browser.

## Troubleshooting

- `ModuleNotFoundError: No module named 'docker'`: you started the backend with your system Python instead of the venv. Run `./start.sh`, or use `backend/.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8001`.
- `Cannot connect to the Docker daemon`: open Docker Desktop and wait for it to finish starting.
- `Image not found` / image pull errors: verify internet access and retry.
- `Unsupported project`: use one of the supported root-level entry points.
- `Dependencies detected`: this MVP intentionally refuses projects requiring external packages. Add a controlled dependency/build stage in a later iteration.
