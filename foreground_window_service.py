"""Windows service host for the foreground-window capture agent.

The service runs in Session 0 and launches the capture script only in the
currently active interactive user's desktop session.  It deliberately does
not capture Session 0, which has no usable foreground window.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SERVICE_NAME = "ForegroundWindowCaptureService"
SERVICE_DISPLAY_NAME = "Foreground Window Capture Service"
RUNTIME_DIR = Path(__file__).resolve().parent
LOG_DIR = RUNTIME_DIR / "logs"
STATE_DIR = RUNTIME_DIR / "state"
STOP_FILE = STATE_DIR / "agent.stop"


def configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(LOG_DIR / "service.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


if sys.platform == "win32":
    import pywintypes
    import servicemanager
    import win32con
    import win32event
    import win32process
    import win32profile
    import win32security
    import win32service
    import win32serviceutil
    import win32ts

    class ForegroundWindowCaptureService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _exe_name_ = str(RUNTIME_DIR / "runtime" / "pythonservice.exe")
        _svc_description_ = (
            "Launches the authorized foreground-window capture agent in the active user session."
        )

        def __init__(self, args: list[str]) -> None:
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, True, False, None)
            self.process: Any | None = None
            self.session_id: int | None = None
            self.logger = configure_logging()

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self) -> None:
            self.SvcStop()

        def SvcDoRun(self) -> None:
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} started")
            self.logger.info("service started")
            try:
                self._supervise()
            except Exception:
                self.logger.exception("service supervision failed")
                servicemanager.LogErrorMsg(f"{SERVICE_NAME} failed; see service.log")
            finally:
                self._stop_agent()
                self.logger.info("service stopped")

        def _supervise(self) -> None:
            while win32event.WaitForSingleObject(self.stop_event, 3000) != win32event.WAIT_OBJECT_0:
                active_session = self._find_active_session()
                if active_session is None:
                    self._stop_agent()
                    continue
                if self._agent_running() and active_session == self.session_id:
                    continue
                self._stop_agent()
                self._start_agent(active_session)

        @staticmethod
        def _find_active_session() -> int | None:
            sessions = win32ts.WTSEnumerateSessions(win32ts.WTS_CURRENT_SERVER_HANDLE)
            active = [
                int(session["SessionId"])
                for session in sessions
                if session["State"] == win32ts.WTSActive
            ]
            try:
                console_id = int(win32ts.WTSGetActiveConsoleSessionId())
                if console_id in active:
                    return console_id
            except pywintypes.error:
                pass
            return active[0] if active else None

        def _start_agent(self, session_id: int) -> None:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STOP_FILE.unlink(missing_ok=True)
            user_token = primary_token = thread_handle = None
            try:
                user_token = win32ts.WTSQueryUserToken(session_id)
                primary_token = win32security.DuplicateTokenEx(
                    user_token, win32security.SecurityImpersonation,
                    win32con.MAXIMUM_ALLOWED, win32security.TokenPrimary, None,
                )
                environment = win32profile.CreateEnvironmentBlock(primary_token, False)
                command = [
                    str(Path(sys.executable).resolve()),
                    str(RUNTIME_DIR / "foreground_window_capture.py"),
                    "--agent", "--stop-file", str(STOP_FILE),
                ]
                startup = win32process.STARTUPINFO()
                startup.lpDesktop = "winsta0\\default"
                startup.dwFlags |= win32con.STARTF_USESHOWWINDOW
                startup.wShowWindow = win32con.SW_HIDE
                process, thread_handle, pid, _ = win32process.CreateProcessAsUser(
                    primary_token, command[0], subprocess.list2cmdline(command),
                    None, None, False, 0x00000400 | 0x08000000, environment,
                    str(RUNTIME_DIR), startup,
                )
                self.process = process
                self.session_id = session_id
                self.logger.info("agent started session=%s pid=%s", session_id, pid)
            finally:
                if thread_handle is not None:
                    thread_handle.Close()
                if primary_token is not None:
                    primary_token.Close()
                if user_token is not None:
                    user_token.Close()

        def _agent_running(self) -> bool:
            return self.process is not None and (
                win32event.WaitForSingleObject(self.process, 0) == win32event.WAIT_TIMEOUT
            )

        def _stop_agent(self) -> None:
            if self.process is None:
                return
            try:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                STOP_FILE.touch()
                if self._agent_running():
                    win32event.WaitForSingleObject(self.process, 10000)
                if self._agent_running():
                    self.logger.warning("agent did not stop in time; terminating")
                    win32process.TerminateProcess(self.process, 1)
            finally:
                self.process.Close()
                self.process = None
                self.session_id = None


def main() -> int:
    if sys.platform != "win32":
        print("This service host must run on Windows.")
        return 1
    win32serviceutil.HandleCommandLine(ForegroundWindowCaptureService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
