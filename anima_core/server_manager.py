"""Optional start/stop of a local llama.cpp server (llama-server).

Useful on a single GPU: the language model only occupies VRAM while you need
it, and it is stopped automatically when you press Generate.

The server listens on the host/port of the "Server URL" setting, so the chat
client and the managed process always agree. Safety rules:
  * a process is only ever stopped if it is the one we started, or a
    leftover process whose executable is named llama-server;
  * nothing is managed when the URL points to another machine;
  * a server we started never outlives the WebUI (see _tie_to_this_process).
"""
from __future__ import annotations

import atexit
import os
import pathlib
import shlex
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .config import llm_base_url

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}

_JOB = None  # Windows job handle; must stay open for the life of the WebUI


def _windows_job():
    """A job object that kills its processes when the WebUI process ends.

    Windows closes the job handle when this process exits for any reason
    (normal exit, closed console window, crash), and with
    KILL_ON_JOB_CLOSE every process in the job is terminated with it.
    """
    global _JOB
    if _JOB is not None:
        return _JOB
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC),
                    ("IoInfo", IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9,  # ExtendedLimitInformation
                                       ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    _JOB = (k32, job)
    return _JOB


def _tie_to_this_process(proc: subprocess.Popen) -> bool:
    """Make the child die together with the WebUI. Returns True on success."""
    if not IS_WINDOWS:
        return True  # handled by _linux_parent_death_signal at spawn time
    try:
        from ctypes import wintypes
        k32, job = _windows_job()
        return bool(k32.AssignProcessToJobObject(wintypes.HANDLE(job),
                                                 wintypes.HANDLE(int(proc._handle))))
    except Exception:
        return False


def _linux_parent_death_signal() -> None:
    """preexec_fn: ask Linux to send SIGTERM to the child if the WebUI dies."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 15)  # PR_SET_PDEATHSIG, SIGTERM
    except Exception:
        pass


def _path(value: str) -> pathlib.Path | None:
    value = (value or "").strip().strip('"')
    if not value:
        return None
    p = pathlib.Path(os.path.expandvars(os.path.expanduser(value)))
    return p if p.is_file() else None


def _popen_flags() -> dict[str, Any]:
    if IS_WINDOWS:
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    flags: dict[str, Any] = {"start_new_session": True}
    if sys.platform.startswith("linux"):
        flags["preexec_fn"] = _linux_parent_death_signal
    return flags


class ServerManager:
    """Manages one llama-server process on the configured port."""

    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda msg: None)
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        # normal WebUI shutdown: stop our server explicitly too
        atexit.register(self._stop_on_exit)

    def _stop_on_exit(self) -> None:
        try:
            if self._ours_running():
                self._kill_tree(self.proc)
        except Exception:
            pass

    # ------------------------------------------------------------------ config
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("local_server", "enabled", default=False))

    def _url(self):
        return urlparse(self.base_url)

    @property
    def host(self) -> str:
        return self._url().hostname or "127.0.0.1"

    @property
    def port(self) -> int:
        u = self._url()
        return int(u.port or (443 if u.scheme == "https" else 80))

    @property
    def is_local(self) -> bool:
        return self.host.lower() in LOCAL_HOSTS

    @property
    def base_url(self) -> str:
        return llm_base_url(self.cfg)

    def _exe(self) -> pathlib.Path | None:
        return _path(str(self.cfg.get("local_server", "llama_server", default="") or ""))

    def _model(self) -> pathlib.Path | None:
        return _path(str(self.cfg.get("local_server", "model", default="") or ""))

    # ------------------------------------------------------------------ state
    def is_up(self, timeout: float = 1.5) -> bool:
        if not self.base_url:
            return False
        try:
            r = requests.get(f"{self.base_url}/models", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def _ours_running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _port_pid(self) -> int | None:
        """PID listening on the port (Windows only; used for leftovers)."""
        if not IS_WINDOWS:
            return None
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=8,
                creationflags=CREATE_NO_WINDOW,
            ).stdout
        except Exception:
            return None
        needle = f":{self.port}"
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local, state, pid = parts[1], parts[3], parts[4]
            if local.endswith(needle) and state.upper() == "LISTENING":
                try:
                    return int(pid)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _proc_name(pid: int) -> str:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=8,
                creationflags=CREATE_NO_WINDOW,
            ).stdout.strip()
        except Exception:
            return ""
        if not out or "INFO" in out.upper():
            return ""
        first = out.splitlines()[0]
        return first.split('"')[1] if first.startswith('"') else first.split(",")[0]

    def _leftover_pid(self) -> int | None:
        """A llama-server left running on our port (e.g. after a WebUI restart)."""
        pid = self._port_pid()
        if pid and "llama-server" in self._proc_name(pid).lower():
            return pid
        return None

    def status(self) -> str:
        if not self.base_url:
            return "⚪ no server URL set"
        up = self.is_up()
        if not up:
            return f"⚪ not reachable · {self.base_url}"
        if self._ours_running():
            return f"🟢 running (started here, pid {self.proc.pid}) · {self.base_url}"
        return f"🟢 running · {self.base_url}"

    # ------------------------------------------------------------------ start
    def start(self, wait: bool = True, timeout: float | None = None) -> str:
        if not self.enabled:
            raise RuntimeError("Local server management is off (Settings → Auto start/stop).")
        if not self.base_url:
            raise RuntimeError("No Server URL set (Settings → Inference server).")
        if self._url().port is None:
            raise RuntimeError(
                f"The Server URL {self.base_url} has no port: write it like "
                "http://127.0.0.1:8080/v1 so llama-server knows where to listen.")
        if not self.is_local:
            raise RuntimeError(f"The server URL points to {self.host}: only local servers can be started.")
        with self._lock:
            if self.is_up():
                return f"Server already running on {self.base_url}"

            # a previous start of ours that never answered: don't leave it behind
            if self._ours_running():
                self._kill_tree(self.proc)
                self.proc = None

            # never start on a port another program is using (e.g. LM Studio)
            busy = self._port_pid()
            if busy:
                name = self._proc_name(busy) or f"pid {busy}"
                raise RuntimeError(
                    f"Port {self.port} is already used by {name}. If that is your inference "
                    "server, turn off Settings → Auto start/stop; otherwise change the port "
                    "in the Server URL.")

            exe = self._exe()
            model = self._model()
            if exe is None:
                raise RuntimeError("llama-server not found: set its path in Settings → Auto start/stop.")
            if model is None:
                raise RuntimeError("Model (.gguf) not found: set it in Settings → Auto start/stop.")

            ctx = int(self.cfg.get("local_server", "ctx", default=8192))
            ngl = int(self.cfg.get("local_server", "ngl", default=99))
            extra = str(self.cfg.get("local_server", "extra_args", default="") or "").strip()
            alias = model.parent.name if model.name == "model.gguf" else model.stem

            cmd = [
                str(exe),
                "-m", str(model),
                "--host", "127.0.0.1" if self.host == "localhost" else self.host,
                "--port", str(self.port),
                "-ngl", str(ngl),
                "-c", str(ctx),
                "--no-webui",
                "--alias", alias,
            ]
            if extra:
                cmd.extend(shlex.split(extra, posix=not IS_WINDOWS))

            self.log(f"starting llama-server on port {self.port} (model: {alias})")
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_popen_flags(),
            )
            if not _tie_to_this_process(self.proc):
                self.log("could not tie llama-server to the WebUI process; "
                         "it may stay running if the WebUI is closed")

        if not wait:
            return "Starting server..."

        limit = timeout if timeout is not None else float(
            self.cfg.get("local_server", "start_timeout", default=120))
        t0 = time.time()
        while time.time() - t0 < limit:
            if self.proc and self.proc.poll() is not None:
                code = self.proc.returncode
                self.proc = None
                raise RuntimeError(
                    f"The server exited immediately (code {code}). "
                    "Check the model and the parameters in Settings."
                )
            if self.is_up(timeout=1.0):
                return f"Server started in {time.time() - t0:.1f}s on {self.base_url}"
            time.sleep(0.4)
        # give up, and don't leave a half-started process holding VRAM
        with self._lock:
            if self._ours_running():
                self._kill_tree(self.proc)
            self.proc = None
        raise RuntimeError(f"The server did not answer after {limit:.0f}s and was stopped.")

    # ---------------------------------------------------------------- stop
    def stop(self, timeout: float = 12.0) -> str:
        """Stop our server, or a leftover llama-server on our port. Nothing else."""
        with self._lock:
            killed = False
            if self._ours_running():
                self._kill_tree(self.proc)
                killed = True
            self.proc = None
            if not killed and self.enabled and self.is_local:
                pid = self._leftover_pid()
                if pid:
                    self._kill_pid(pid)
                    killed = True

        if not killed:
            if self.is_up(timeout=0.8):
                return "The server was not started here and is not a llama-server: left running."
            return "The server was already stopped."
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.is_up(timeout=0.8):
                return "Server stopped."
            time.sleep(0.3)
        return "Stop requested, but the port still answers."

    def stop_if_managed(self) -> bool:
        """Called before Generate: stop only what this extension manages."""
        if not (self.enabled and self.is_local):
            return False
        if self._ours_running() or (self.is_up(timeout=0.6) and self._leftover_pid()):
            self.stop(timeout=8)
            return True
        return False

    def _kill_tree(self, proc: subprocess.Popen) -> None:
        if IS_WINDOWS:
            self._kill_pid(proc.pid)
        else:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    @staticmethod
    def _kill_pid(pid: int) -> None:
        if IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=15,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception:
                pass
        else:
            try:
                os.kill(pid, 15)
            except Exception:
                pass
