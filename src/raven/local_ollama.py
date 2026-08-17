from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import ollama
from curl_cffi import requests

from ._paths import ollama_root


DEFAULT_OLLAMA_VERSION = "0.32.9"


class LocalOllama:
    def __init__(
        self,
        root: Path | None = None,
        host: str = "127.0.0.1",
        version: str | None = None,
        backend: str | None = None,
    ) -> None:
        self.root = (root or ollama_root()).resolve()
        self.host = host
        self.version = version or os.environ.get("OLLAMA_VERSION") or DEFAULT_OLLAMA_VERSION
        # backend: "" (auto) | "cuda" | "vulkan" | "cpu". Default from env OLLAMA_BACKEND/RAVEN_OLLAMA_BACKEND.
        self.backend = (
            backend or os.environ.get("OLLAMA_BACKEND") or os.environ.get("RAVEN_OLLAMA_BACKEND") or ""
        ).strip().lower()
        self.port = self._find_free_port()
        self.bin_path = self.root / "ollama" / "bin" / ("ollama.exe" if os.name == "nt" else "ollama")
        self.home = self.root / "ollama" / "home"
        self.models = self.root / "ollama" / "models"
        self.logs = self.root / "ollama" / "logs"
        self._proc: subprocess.Popen[str] | None = None
        self._stdout: object | None = None
        self._stderr: object | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["OLLAMA_HOST"] = f"{self.host}:{self.port}"
        env["OLLAMA_HOME"] = str(self.home)
        env["OLLAMA_MODELS"] = str(self.models)
        if self.backend == "vulkan":
            # Force the Vulkan runner hard: OLLAMA_LLM_LIBRARY=<dir> makes GPU
            # discovery load ONLY the matching lib (cuda_v12/cuda_v13 are skipped),
            # and OLLAMA_VULKAN=1 keeps Vulkan enabled. Avoids the CUDA runner
            # crashes (cudaMalloc/PTX JIT) seen on this machine.
            env["OLLAMA_LLM_LIBRARY"] = "vulkan"
            env["OLLAMA_VULKAN"] = "1"
        elif self.backend == "cuda":
            env["OLLAMA_VULKAN"] = "0"
        elif self.backend == "cpu":
            env["OLLAMA_VULKAN"] = "0"
            env["CUDA_VISIBLE_DEVICES"] = "-1"
            env["GGML_VK_VISIBLE_DEVICES"] = "-1"
        return env

    def _find_free_port(self) -> int:
        with socket.socket() as s:
            s.bind((self.host, 0))
            return s.getsockname()[1]

    @property
    def _lib_dir(self) -> Path:
        return self.root / "ollama" / "bin" / "lib" / "ollama"

    @property
    def _arch(self) -> str:
        m = platform.machine().lower()
        if m in ("amd64", "x86_64"):
            return "amd64"
        if m in ("arm64", "aarch64"):
            return "arm64"
        raise RuntimeError(f"unsupported platform arch: {m}")

    @property
    def _asset_name(self) -> str:
        if os.name == "nt":
            return f"ollama-windows-{self._arch}.zip"
        if sys.platform == "darwin":
            return "ollama-darwin.tgz"
        return f"ollama-linux-{self._arch}.tar.zst"

    @property
    def _asset_url(self) -> str:
        override = os.environ.get("OLLAMA_BINARY_URL")
        if override:
            return override
        return (
            f"https://github.com/ollama/ollama/releases/download/"
            f"v{self.version}/{self._asset_name}"
        )

    @property
    def _checksum_url(self) -> str:
        override = os.environ.get("OLLAMA_CHECKSUM_URL")
        if override:
            return override
        return (
            f"https://github.com/ollama/ollama/releases/download/"
            f"v{self.version}/sha256sum.txt"
        )

    async def start(self, on_progress=None) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return False
        downloaded = False
        if not self.bin_path.is_file():
            await asyncio.to_thread(self._download_binary, on_progress)
            downloaded = True
        self.home.mkdir(parents=True, exist_ok=True)
        self.models.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        # A previous run may have been hard-killed (Ctrl+C / crash), leaving
        # orphaned GPU runners from this bundle squatting on VRAM. Sweep first
        # so they never starve the server we are about to launch.
        await asyncio.to_thread(self._sweep_orphan_runners)
        self._stdout = (self.logs / "server.log").open("w")
        self._stderr = (self.logs / "server.err.log").open("w")
        self._proc = subprocess.Popen(
            [str(self.bin_path), "serve"],
            env=self.env,
            stdout=self._stdout,
            stderr=self._stderr,
            text=True,
        )
        await self._wait_healthy()
        return downloaded

    def _download_binary(self, on_progress=None) -> None:
        cache_dir = self.root / "ollama" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        archive = cache_dir / self._asset_name
        try:
            self._download(self._asset_url, archive, on_progress)
            self._verify_checksum(archive)
            self._install(archive)
        finally:
            if archive.exists():
                archive.unlink()

    def _download(self, url: str, dest: Path, on_progress=None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.Session() as session:
            session.headers.update({"User-Agent": f"raven/{self.version}"})
            resp = session.get(
                url,
                stream=True,
                impersonate="chrome120",
                timeout=30,
            )
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done, total)

    def _verify_checksum(self, archive: Path) -> None:
        with requests.Session() as session:
            session.headers.update({"User-Agent": f"raven/{self.version}"})
            resp = session.get(
                self._checksum_url,
                impersonate="chrome120",
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.text
        expected = None
        for line in text.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip().endswith(self._asset_name):
                expected = parts[0].lower()
                break
        if not expected:
            raise RuntimeError(f"checksum for {self._asset_name} not found in {self._checksum_url}")
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"sha256 mismatch for {self._asset_name} (expected {expected}, got {actual})")

    def _install(self, archive: Path) -> None:
        temp = Path(tempfile.mkdtemp(prefix="ollama-bundle-"))
        try:
            self._extract(archive, temp)
            binary, lib = self._locate(temp)
            target_dir = self.bin_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(binary), str(self.bin_path))
            if lib is not None:
                dest_lib = target_dir / "lib"
                if dest_lib.exists():
                    shutil.rmtree(dest_lib)
                shutil.move(str(lib), str(dest_lib))
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def _extract(self, archive: Path, temp: Path) -> None:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                self._safe_extract_zip(z, temp)
        elif archive.name.endswith(".tgz"):
            with tarfile.open(archive, "r:*") as t:
                self._safe_extract_tar(t, temp)
        elif archive.name.endswith(".tar.zst"):
            import zstandard as zstd  # type: ignore[import-not-found]

            tar_path = temp / "bundle.tar"
            with open(archive, "rb") as src, open(tar_path, "wb") as dst:
                zstd.ZstdDecompressor().copy_stream(src, dst)
            with tarfile.open(tar_path, "r:") as t:
                self._safe_extract_tar(t, temp)
        else:
            raise RuntimeError(f"unsupported archive format: {archive.name}")

    @staticmethod
    def _safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
        root = destination.resolve()
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise RuntimeError(f"archive links are not allowed: {member.name}")
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"archive member escapes extraction directory: {member.name}")
        archive.extractall(destination)

    @staticmethod
    def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
        root = destination.resolve()
        for member in archive.infolist():
            file_type = (member.external_attr >> 16) & 0o170000
            if file_type == 0o120000:
                raise RuntimeError(f"archive links are not allowed: {member.filename}")
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"archive member escapes extraction directory: {member.filename}")
        archive.extractall(destination)

    def _locate(self, temp: Path) -> tuple[Path, Path | None]:
        exe = "ollama.exe" if os.name == "nt" else "ollama"
        candidates = [temp / exe, temp / "bin" / exe, temp / "bin" / "ollama"]
        binary = next((p for p in candidates if p.is_file()), None)
        if binary is None:
            raise RuntimeError(f"could not locate ollama binary in {temp}")
        lib = temp / "lib"
        return binary, lib if lib.is_dir() else None

    async def _wait_healthy(self, timeout: float = 60.0) -> None:
        client = ollama.AsyncClient(host=self.base_url)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                raise RuntimeError(
                    f"ollama server exited early (code "
                    f"{self._proc.returncode if self._proc else '?'}). "
                    f"See {self.logs / 'server.err.log'}"
                )
            try:
                await client.list()
                return
            except Exception:
                await asyncio.sleep(0.5)
        raise TimeoutError(f"ollama server did not become ready within {timeout}s")

    def _sweep_orphan_runners(self) -> None:
        """Kill leftover GPU runners left behind by abruptly-stopped servers.

        Ollama spawns llama-server (the GPU backend process) itself; killing
        the ollama parent does NOT cascade on Windows (no process groups), so a
        terminate/kill leaves the runners orphaned and alive. A still-running
        runner keeps its GPU context/pilots allocation, so every leak starves
        the next run (cudaMalloc OOM) despite the model fitting in VRAM.

        The sweep is scoped to this bundle's lib dir and can never touch a
        system Ollama install (e.g. Program Files) or its modelserver.
        """

        def _kill(pid: int) -> None:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

        if os.name == "nt":
            marker = str(self._lib_dir).replace("'", "''")
            script = (
                "$p = '" + marker + "'; "
                "Get-CimInstance Win32_Process -Filter \"Name = 'llama-server.exe'\" | "
                "Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($p) } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                pass
            return
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=15
            ).stdout
        except Exception:
            return
        runner = "llama-server"
        marker = str(self._lib_dir)
        for line in out.splitlines():
            split = line.strip().split(None, 1)
            if len(split) != 2 or runner not in split[1] or marker not in split[1]:
                continue
            try:
                _kill(int(split[0]))
            except ValueError:
                pass

    async def stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = None
        else:
            self._proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self._proc.wait), 5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await asyncio.to_thread(self._proc.wait)
            self._proc = None
        # Terminate is abrupt on Windows: ollama never got to shut down its
        # llama-server children, so sweep any runners left under the bundle.
        await asyncio.to_thread(self._sweep_orphan_runners)
