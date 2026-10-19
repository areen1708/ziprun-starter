from __future__ import annotations

import io
import zipfile
from pathlib import Path

import docker
import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def _docker_available() -> bool:
    try:
        cli = docker.from_env()
        cli.ping()
        cli.close()
        return True
    except Exception:
        return False


DOCKER_AVAILABLE = _docker_available()
requires_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker daemon is not available")


def build_zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def post_upload(filename: str, data: bytes):
    return client.post("/api/run", files={"file": (filename, data, "application/octet-stream")})


class TestUploadValidation:
    def test_health_reports_docker(self):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["docker"] == "connected"

    def test_rejects_unsupported_file_type(self):
        response = post_upload("notes.txt", b"hello")
        assert response.status_code == 400
        assert ".zip" in response.json()["detail"]

    def test_rejects_missing_file(self):
        response = client.post("/api/run")
        assert response.status_code == 422

    def test_rejects_empty_upload(self):
        response = post_upload("main.py", b"")
        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_rejects_oversized_upload(self, monkeypatch):
        monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 16)
        response = post_upload("main.py", b"print('hi')" * 100)
        assert response.status_code == 413

    def test_rejects_invalid_zip(self):
        response = post_upload("project.zip", b"this is not a zip file")
        assert response.status_code == 400
        assert "not a valid ZIP" in response.json()["detail"]

    def test_rejects_path_traversal(self):
        response = post_upload("evil.zip", build_zip({"../escape.py": "print(1)"}))
        assert response.status_code == 400
        assert "unsafe" in response.json()["detail"]

    def test_rejects_absolute_path(self):
        response = post_upload("evil.zip", build_zip({"/etc/passwd": "x"}))
        assert response.status_code == 400

    def test_rejects_symlink_entries(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            info = zipfile.ZipInfo("link.py")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
        response = post_upload("evil.zip", buffer.getvalue())
        assert response.status_code == 400
        assert "symbolic link" in response.json()["detail"]

    def test_rejects_too_many_files(self, monkeypatch):
        monkeypatch.setattr(main, "MAX_FILES", 2)
        response = post_upload("many.zip", build_zip({"a.py": "1", "b.py": "2", "c.py": "3"}))
        assert response.status_code == 400
        assert "too many entries" in response.json()["detail"]


class TestDetection:
    def test_missing_entry_point(self):
        response = post_upload("project.zip", build_zip({"README.md": "# hi", "docs/guide.md": "x"}))
        assert response.status_code == 422
        assert "entry point" in response.json()["detail"].lower()

    def test_ambiguous_source_files(self):
        response = post_upload(
            "project.zip",
            build_zip({"alpha.py": "print(1)", "beta.py": "print(2)"}),
        )
        assert response.status_code == 422
        assert "multiple source files" in response.json()["detail"].lower()

    def test_dockerfile_is_rejected(self):
        response = post_upload(
            "project.zip",
            build_zip({"Dockerfile": "FROM python:3.12", "main.py": "print(1)"}),
        )
        assert response.status_code == 422
        assert "Dockerfile" in response.json()["detail"]

    def test_package_json_with_dependencies_is_rejected(self):
        response = post_upload(
            "project.zip",
            build_zip({"package.json": '{"dependencies": {"left-pad": "1.0.0"}}', "index.js": "console.log(1)"}),
        )
        assert response.status_code == 422
        assert "dependencies" in response.json()["detail"].lower()

    def test_invalid_package_json(self):
        response = post_upload("project.zip", build_zip({"package.json": "{not json", "index.js": "console.log(1)"}))
        assert response.status_code == 422

    def test_locate_project_root_unwraps_single_directory(self, tmp_path: Path):
        root = tmp_path / "extracted"
        (root / "repo-main" / "nested").mkdir(parents=True)
        (root / "repo-main" / "main.py").write_text("print(1)")
        assert main.locate_project_root(root) == root / "repo-main"

    def test_locate_project_root_ignores_macos_junk(self, tmp_path: Path):
        root = tmp_path / "extracted"
        (root / "repo-main").mkdir(parents=True)
        (root / "repo-main" / "main.py").write_text("print(1)")
        (root / ".DS_Store").write_bytes(b"junk")
        assert main.locate_project_root(root) == root / "repo-main"

    def test_detect_plan_prefers_main_py(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("print(1)")
        (tmp_path / "index.js").write_text("console.log(1)")
        plan = main.detect_plan(tmp_path)
        assert plan.language == "Python"
        assert plan.entry == "main.py"

    def test_detect_plan_single_named_python_file(self, tmp_path: Path):
        (tmp_path / "test.py").write_text("print(1)")
        plan = main.detect_plan(tmp_path)
        assert plan.language == "Python"
        assert plan.entry == "test.py"

    def test_detect_plan_nested_project_directory(self, tmp_path: Path):
        nested = tmp_path / "my-app" / "src"
        nested.mkdir(parents=True)
        (nested / "main.py").write_text("print(1)")
        plan = main.detect_plan(tmp_path)
        assert plan.language == "Python"
        assert plan.workdir == "my-app/src"

    def test_detect_plan_duplicate_main_py(self, tmp_path: Path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        (tmp_path / "a" / "main.py").write_text("print(1)")
        (tmp_path / "b" / "main.py").write_text("print(2)")
        with pytest.raises(ValueError, match="Multiple main.py"):
            main.detect_plan(tmp_path)

    def test_commands_never_use_unquoted_user_input(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("print(1)")
        plan = main.detect_plan(tmp_path)
        assert plan.command.startswith("mkdir -p /tmp/project")
        assert "python main.py" in plan.command


@requires_docker
class TestExecution:
    def test_python_single_file_upload(self):
        response = post_upload("hello.py", b"print('Hello from ZipRun!')\nprint(2 + 3)\n")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "success"
        assert payload["exit_code"] == 0
        assert payload["language"] == "Python"
        assert "Hello from ZipRun!" in payload["output"]
        assert "5" in payload["output"]

    def test_python_zip_upload(self):
        response = post_upload("project.zip", build_zip({"main.py": "print('zip main.py ok')"}))
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "success"
        assert "zip main.py ok" in payload["output"]

    def test_python_named_file_zip_upload(self):
        response = post_upload(
            "project.zip",
            build_zip({"test.py": "print('named file ok')", "__MACOSX/._test.py": "junk"}),
        )
        assert response.status_code == 200, response.text
        assert "named file ok" in response.json()["output"]

    def test_nested_zip_upload(self):
        response = post_upload("project.zip", build_zip({"my-repo/main.py": "print('nested ok')"}))
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["workdir"] == "."
        assert "nested ok" in payload["output"]

    def test_deeply_nested_zip_upload(self):
        response = post_upload(
            "project.zip",
            build_zip({"app/main.py": "print('deep ok')", "docs/notes.txt": "ignore"}),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["workdir"] == "app"
        assert "deep ok" in payload["output"]

    def test_zip_path_with_spaces(self):
        response = post_upload(
            "project.zip",
            build_zip({"my app/main.py": "print('space ok')", "docs/notes.txt": "ignore"}),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["workdir"] == "my app"
        assert "'/tmp/project/my app'" in payload["command"]
        assert "space ok" in payload["output"]

    def test_node_zip_upload(self):
        response = post_upload("project.zip", build_zip({"index.js": "console.log('node ok')"}))
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["language"] == "Node.js"
        assert "node ok" in payload["output"]

    def test_cpp_zip_upload(self):
        response = post_upload(
            "project.zip",
            build_zip({"main.cpp": "#include <iostream>\nint main(){ std::cout << \"cpp ok\\n\"; return 0; }\n"}),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["language"] == "C++"
        assert "cpp ok" in payload["output"]

    def test_java_zip_upload(self):
        source = "public class Main { public static void main(String[] a){ System.out.println(\"java ok\"); } }\n"
        response = post_upload("project.zip", build_zip({"Main.java": source}))
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["language"] == "Java"
        assert "java ok" in payload["output"]

    def test_stderr_is_captured(self):
        response = post_upload(
            "main.py",
            b"import sys\nprint('to stderr', file=sys.stderr)\nsys.exit(0)\n",
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert "to stderr" in payload["stderr"]
        assert "to stderr" in payload["output"]

    def test_execution_failure_returns_error(self):
        response = post_upload("main.py", b"import sys\nprint('boom')\nsys.exit(3)\n")
        assert response.status_code == 500
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["exit_code"] == 3
        assert "boom" in payload["output"]
        assert "detail" in payload

    def test_python_crash_returns_error(self):
        response = post_upload("main.py", b"raise ValueError('kaput')\n")
        assert response.status_code == 500
        payload = response.json()
        assert payload["status"] == "error"
        assert "ValueError" in payload["output"]

    def test_compilation_failure_returns_error(self):
        response = post_upload("project.zip", build_zip({"main.cpp": "int main() { return }"}))
        assert response.status_code == 500
        payload = response.json()
        assert payload["status"] == "error"
        assert "error" in payload["output"].lower()

    def test_timeout_returns_408(self, monkeypatch):
        monkeypatch.setattr(main, "MAX_RUNTIME_SECONDS", 3)
        response = post_upload("main.py", b"import time\nwhile True:\n    time.sleep(0.5)\n")
        assert response.status_code == 408
        payload = response.json()
        assert payload["status"] == "timeout"
        assert payload["timed_out"] is True
        assert payload["exit_code"] != 0

    def test_container_is_removed_after_run(self):
        assert post_upload("main.py", b"print('cleanup check')\n").status_code == 200
        cli = docker.from_env()
        try:
            remaining = [
                container
                for container in cli.containers.list(all=True)
                if (container.labels or {}).get("app") == "ziprun"
            ]
        finally:
            cli.close()
        assert remaining == []
