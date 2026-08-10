#!/usr/bin/env python3
"""
Developer Activity Agent — data collection daemon.
Tracks: active app, local git commits, AI tools, VS Code extensions,
        Claude Code token usage.
Sends to the Developer Activity backend via device token.

First run: token is read from OS keyring (set by auth.py after SSO).
"""
import json
import logging
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

import psutil
import requests
import keyring


_NO_WINDOW = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if platform.system() == "Windows" else {}
)

KEYRING_SERVICE   = "da-agent"
KEYRING_TOKEN_KEY = "device-token"
KEYRING_URL_KEY   = "backend-url"

DEFAULT_BACKEND = os.environ.get("DA_BACKEND", "http://localhost:8000")

HEARTBEAT_INTERVAL  = 30
AI_CHECK_INTERVAL   = 60
AI_RESEND_INTERVAL  = 120
EXTENSION_INTERVAL  = 600
CLAUDE_INTERVAL     = 60
DISCOVER_INTERVAL   = 180
STANDUP_INTERVAL    = 300
REPO_SCAN_INTERVAL  = 300
_MAX_WATCHED_REPOS  = 25

_DEFAULT_PROC_MAP: dict[str, str] = {
    "cursor":        "cursor-ai",
    "windsurf":      "windsurf",
    "claude":        "claude-code",
    "copilot":       "github-copilot",
    "codeium":       "codeium",
    "tabnine":       "tabnine",
    "supermaven":    "supermaven",
    "continue":      "continue-dev",
    "aider":         "aider",
    "amazonq":       "amazon-q",
    "codewhisperer": "amazon-q",
    "gemini":        "gemini-cli",
    "ollama":        "ollama",
    "granola":       "granola",
}

_DEFAULT_VSCODE_EXT_MAP: dict[str, str] = {
    "github.copilot":                            "github-copilot",
    "github.copilot-chat":                       "github-copilot",
    "anysphere.cursor-always-local":             "cursor-ai",
    "codeium.codeium":                           "codeium",
    "codeium.windsurf":                          "windsurf",
    "tabnine.tabnine-vscode":                    "tabnine",
    "continue.continue":                         "continue-dev",
    "amazonwebservices.aws-toolkit-vscode":      "amazon-q",
    "amazonwebservices.amazon-q-vscode":         "amazon-q",
    "supermaven.supermaven":                     "supermaven",
    "google.geminicodeassist":                   "gemini-code-assist",
    "saoudrizwan.claude-dev":                    "cline",
    "rooveterinaryinc.roo-cline":                "roo-cline",
    "anthropic.claude":                          "claude-code",
}

_DEFAULT_AI_KEYWORDS = {"gpt", "llm", "coder", "codex", "assistant", "autocomplete", "intellicode"}

AI_PROC_MAP:    dict[str, str] = dict(_DEFAULT_PROC_MAP)
VSCODE_EXT_MAP: dict[str, str] = dict(_DEFAULT_VSCODE_EXT_MAP)
_AI_KEYWORDS:   set[str]       = set(_DEFAULT_AI_KEYWORDS)


def _refresh_tool_definitions(backend: str) -> bool:
    """Fetch tool detection maps from the server. Returns True on success."""
    try:
        r = requests.get(f"{backend}/api/agent/tool-definitions", timeout=8)
        if r.status_code != 200:
            return False
        data = r.json()
        AI_PROC_MAP.clear()
        AI_PROC_MAP.update(data.get("proc_map", _DEFAULT_PROC_MAP))
        VSCODE_EXT_MAP.clear()
        VSCODE_EXT_MAP.update(data.get("vscode_ext_map", _DEFAULT_VSCODE_EXT_MAP))
        _AI_KEYWORDS.clear()
        _AI_KEYWORDS.update(data.get("keywords", _DEFAULT_AI_KEYWORDS))
        log.info("Tool definitions refreshed: %d proc keywords, %d vscode extensions",
                 len(AI_PROC_MAP), len(VSCODE_EXT_MAP))
        return True
    except Exception as e:
        log.debug("Tool definition fetch failed: %s", e)
        return False


def load_token() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)

def load_backend() -> str:
    return keyring.get_password(KEYRING_SERVICE, KEYRING_URL_KEY) or DEFAULT_BACKEND


def _best_cwd(proc: "psutil.Process") -> Path | None:
    try:
        for child in proc.children(recursive=True):
            try:
                cwd = Path(child.cwd())
                for p in [cwd, *cwd.parents]:
                    if (p / ".git").is_dir():
                        return p
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        return Path(proc.cwd())
    except Exception:
        return None


def get_active_info() -> tuple[str, Path | None]:
    """Returns (app_name, repo_root_or_None)."""
    if platform.system() == "Windows":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd   = user32.GetForegroundWindow()
            pid    = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = psutil.Process(pid.value)
            return proc.name(), _best_cwd(proc)
        except Exception:
            return "unknown", None
    elif platform.system() == "Darwin":
        try:
            r = subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=3,
            )
            return r.stdout.strip() or "unknown", None
        except Exception:
            return "unknown", None
    return "unknown", None


def get_idle_seconds() -> int:
    if platform.system() != "Windows":
        return 0
    try:
        import ctypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        ticks = ctypes.windll.kernel32.GetTickCount()
        return max(0, (ticks - lii.dwTime) // 1000)
    except Exception:
        return 0


_EXCLUDED_REPO_PARTS = ("\\windows\\", "\\program files", "\\programdata\\", "node_modules")


def _is_trackable_repo(root: Path) -> bool:
    s = str(root).lower()
    return not any(part in s for part in _EXCLUDED_REPO_PARTS)


def _repo_root(path: Path | None) -> Path | None:
    """First .git dir at or above `path`, or None if there isn't one / it's excluded.
    Stops at the first .git so a system checkout (e.g. C:\\Windows) can't be reported."""
    if not path:
        return None
    for p in [path, *path.parents]:
        if (p / ".git").is_dir():
            return p if _is_trackable_repo(p) else None
    return None


def _branch_of(root: Path) -> str:
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: refs/heads/"):
            return head[len("ref: refs/heads/"):]
    except Exception:
        pass
    return "unknown"


def find_git_repo(path: Path | None) -> tuple[str | None, str | None]:
    root = _repo_root(path)
    return (root.name, _branch_of(root)) if root else (None, None)


def discover_repos() -> set[Path]:
    """Git repos any running process is sitting in — focus-independent discovery, so
    commits are tracked no matter which window (VS Code, terminal) is on top."""
    found: set[Path] = set()
    for proc in psutil.process_iter(["cwd"]):
        try:
            cwd = proc.info.get("cwd")
            if not cwd:
                continue
            root = _repo_root(Path(cwd))
            if root:
                found.add(root)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
    return found


def get_new_commits(repo_path: Path, since_sha: str | None) -> list[dict]:
    try:
        if since_sha:
            cmd = ["git", "log", f"{since_sha}..HEAD", "--format=%H|%s|%aI", "--no-merges"]
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            cmd   = ["git", "log", f"--after={today} 00:00", "--format=%H|%s|%aI", "--no-merges"]

        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=5, **_NO_WINDOW)
        if result.returncode != 0 or not result.stdout.strip():
            return []

        commits = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) < 2:
                continue
            sha, message = parts[0], parts[1]
            stats = subprocess.run(
                ["git", "show", "--stat", "--format=", sha],
                cwd=repo_path, capture_output=True, text=True, timeout=5, **_NO_WINDOW,
            )
            files_changed = insertions = deletions = 0
            for stat_line in stats.stdout.splitlines():
                if "changed" in stat_line:
                    m = re.search(r"(\d+) file", stat_line)
                    if m: files_changed = int(m.group(1))
                    m = re.search(r"(\d+) insertion", stat_line)
                    if m: insertions = int(m.group(1))
                    m = re.search(r"(\d+) deletion", stat_line)
                    if m: deletions = int(m.group(1))
            commits.append({
                "sha": sha[:12], "full_sha": sha,
                "message": message[:500],
                "files_changed": files_changed,
                "insertions": insertions, "deletions": deletions,
            })
        return commits
    except Exception:
        return []


_AI_DATA_DIRS: dict[str, str] = {
    ".claude":   "claude-code",
    ".codex":    "codex",
    ".cursor":   "cursor-ai",
    ".gemini":   "gemini-cli",
    ".aider":    "aider",
    ".continue": "continue-dev",
    ".codeium":  "windsurf",
    ".ollama":   "ollama",
    ".tabnine":  "tabnine",
}
_AI_DIR_KEYWORDS = ("claude", "codex", "cursor", "copilot", "gemini", "gpt",
                    "aider", "windsurf", "codeium", "ollama", "tabnine",
                    "anthropic", "openai", "llm")
_AI_DATA_FRESH_SECS = 900


def _tool_name_for_dir(dirname: str) -> str | None:
    """Home dot-dir name -> tool name: known dir gets a clean name, unknown but
    AI-named dir surfaces raw (dot stripped); anything else is None."""
    if not dirname.startswith("."):
        return None
    low = dirname.lower()
    if low in _AI_DATA_DIRS:
        return _AI_DATA_DIRS[low]
    if any(k in low for k in _AI_DIR_KEYWORDS):
        return dirname.lstrip(".")
    return None


def _touched_recently(path: Path, cutoff: float, cap: int = 1500) -> bool:
    """True if any file under `path` has mtime >= cutoff. Bounded to `cap` files so a
    huge history dir can't stall the loop; returns on the first fresh file, so an
    actively-used tool is cheap."""
    seen = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                if os.stat(os.path.join(root, f)).st_mtime >= cutoff:
                    return True
            except OSError:
                pass
            seen += 1
            if seen >= cap:
                return False
    return False


def discover_local_ai_tools(fresh_secs: int = _AI_DATA_FRESH_SECS) -> list[str]:
    """Auto-find AI tools by the data they save under the home folder — no per-tool
    setup. Scans only top-level dot-dirs (fast; that's where tools live). Reports a
    tool only if its data was written within `fresh_secs`, so this reflects use."""
    home = Path.home()
    cutoff = time.time() - fresh_secs
    found: set[str] = set()
    try:
        entries = list(home.iterdir())
    except OSError:
        return []
    for d in entries:
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        tool = _tool_name_for_dir(d.name)
        if tool and _touched_recently(d, cutoff):
            found.add(tool)
    return sorted(found)


def detect_ai_tools(installed_extensions: list[str] | None = None) -> list[str]:
    detected: set[str] = set()

    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (proc.info["name"] or "").lower().replace(".exe", "")
                for keyword, tool in AI_PROC_MAP.items():
                    if keyword in name:
                        detected.add(tool)
                        break
                else:
                    if any(kw in name for kw in _AI_KEYWORDS):
                        detected.add(name)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    for ext_id in (installed_extensions or []):
        tool = VSCODE_EXT_MAP.get(ext_id.lower())
        if tool:
            detected.add(tool)

    return sorted(detected)


def _repo_from_project_dir(dirname: str) -> str:
    parts = [p for p in re.split(r"-+", dirname) if p]
    return parts[-1] if parts else dirname


def _files_from_content(content) -> list[str]:
    """Extract file paths from tool_use blocks in an assistant message."""
    if not isinstance(content, list):
        return []
    files = []
    for block in content:
        if block.get("type") == "tool_use":
            inp = block.get("input", {})
            fp  = inp.get("file_path") or inp.get("path")
            if fp and isinstance(fp, str):
                files.append(fp)
    return files


_CUTOFF_DAYS = 30


def get_claude_usage() -> list[dict]:
    """Full scan — stateless, idempotent. Skips files untouched > 30 days."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []

    cutoff_mtime = time.time() - _CUTOFF_DAYS * 86400
    from datetime import timedelta
    cutoff_date  = (datetime.now() - timedelta(days=_CUTOFF_DAYS)).strftime("%Y-%m-%d")

    agg: dict[tuple[str, str, str], dict] = {}

    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        repo = _repo_from_project_dir(project_dir.name)
        for jsonl_file in project_dir.rglob("*.jsonl"):
            try:
                if jsonl_file.stat().st_mtime < cutoff_mtime:
                    continue
                with jsonl_file.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("type") != "assistant":
                            continue
                        msg   = entry.get("message") or {}
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        ts = entry.get("timestamp", "")
                        try:
                            ldt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                        except Exception:
                            ldt = datetime.now()
                        date_str = ldt.strftime("%Y-%m-%d")
                        if date_str < cutoff_date:
                            continue
                        model = (msg.get("model") or "claude-sonnet").lower()
                        key   = (date_str, model, repo)
                        if key not in agg:
                            agg[key] = {"input": 0, "cache_w": 0, "cache_r": 0,
                                        "output": 0, "messages": 0, "files": set(), "hours": {}}
                        a = agg[key]
                        in_tok  = usage.get("input_tokens", 0)
                        out_tok = usage.get("output_tokens", 0)
                        a["input"]    += in_tok
                        a["cache_w"]  += usage.get("cache_creation_input_tokens", 0)
                        a["cache_r"]  += usage.get("cache_read_input_tokens", 0)
                        a["output"]   += out_tok
                        a["messages"] += 1
                        hb = a["hours"].setdefault(ldt.hour, {"in": 0, "out": 0})
                        hb["in"]  += in_tok
                        hb["out"] += out_tok
                        for fp in _files_from_content(msg.get("content", [])):
                            a["files"].add(fp)
            except Exception:
                pass

    return [
        {
            "date": date, "model": model, "repo": repo,
            "input_tokens":          v["input"],
            "cache_creation_tokens": v["cache_w"],
            "cache_read_tokens":     v["cache_r"],
            "output_tokens":         v["output"],
            "message_count":         v["messages"],
            "files":                 sorted(v["files"]),
            "hourly": [
                {"hour": h, "input_tokens": hv["in"], "output_tokens": hv["out"]}
                for h, hv in sorted(v["hours"].items())
            ],
        }
        for (date, model, repo), v in agg.items() if v["messages"] > 0
    ]


def get_vscode_extensions() -> list[str]:
    for cmd in (["code", "--list-extensions"], ["cursor", "--list-extensions"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, **_NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                return sorted({e.strip() for e in r.stdout.splitlines() if e.strip()})
        except Exception:
            pass
    candidates = [Path.home() / ".vscode" / "extensions"]
    if platform.system() == "Windows":
        up = os.environ.get("USERPROFILE", "")
        if up:
            candidates.append(Path(up) / ".vscode" / "extensions")
    for d in candidates:
        if d.is_dir():
            return sorted(x.name for x in d.iterdir() if x.is_dir() and not x.name.startswith("."))
    return []


class AgentClient:
    def __init__(self, token: str, backend: str):
        self._headers = {"Authorization": f"Bearer {token}"}
        self._base    = backend.rstrip("/")

    def _post(self, path: str, payload: dict) -> bool:
        try:
            r = requests.post(
                f"{self._base}{path}", json=payload,
                headers=self._headers, timeout=8,
            )
            if r.status_code == 401:
                log.warning("401 from server — token may be revoked")
            return r.status_code == 200
        except Exception as e:
            log.debug("POST %s failed: %s", path, e)
            return False

    def ping(self) -> bool:
        try:
            r = requests.get(
                f"{self._base}/api/agent/status",
                headers=self._headers, timeout=5,
            )
            return r.status_code == 200
        except Exception:
            return False

    def heartbeat(self, app: str, repo: str | None, branch: str | None, idle: bool) -> bool:
        return self._post("/api/agent/heartbeat", {
            "active_app": app, "git_repo": repo,
            "git_branch": branch, "idle": idle,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def commit(self, repo: str, branch: str, c: dict) -> bool:
        return self._post("/api/agent/commit", {
            "repo": repo, "branch": branch,
            "sha": c["full_sha"], "message": c["message"],
            "files_changed": c["files_changed"],
            "insertions": c["insertions"], "deletions": c["deletions"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def ai_event(self, tools: list[str]) -> bool:
        return self._post("/api/agent/ai-event", {
            "tools": tools,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def claude_usage(self, entries: list[dict]) -> bool:
        if not entries:
            return True
        return self._post("/api/agent/claude-usage", {"entries": entries})

    def vscode_extensions(self, extensions: list[str]) -> bool:
        return self._post("/api/agent/vscode-extensions", {"extensions": extensions})

    def get_pending_standup(self) -> dict | None:
        try:
            r = requests.get(
                f"{self._base}/api/agent/standup/pending",
                headers=self._headers, timeout=8,
            )
            if r.status_code == 200:
                return r.json().get("standup")
        except Exception as e:
            log.debug("standup pending fetch failed: %s", e)
        return None

    def ack_standup(self, date: str) -> bool:
        return self._post("/api/agent/standup/ack", {"date": date})


def _should_notify(standup: dict | None, last_date: str | None) -> bool:
    """True when there is a pending standup we haven't shown yet."""
    return bool(standup) and standup.get("date") != last_date


def run(token: str, backend: str, on_status=None, stop_event=None, on_notify=None):
    """
    Main collection loop. on_status(connected: bool) updates the tray indicator.
    on_notify(title, body) shows a desktop toast (standup delivery, ADR-0002).
    stop_event (threading.Event) signals a clean shutdown.
    """
    client = AgentClient(token, backend)
    log.info("Agent starting — backend=%s", backend)

    if not backend.startswith("https://") and not any(h in backend for h in ("localhost", "127.0.0.1")):
        log.warning("Backend %s is not HTTPS — tool definitions and standup text are "
                    "fetched in cleartext and can be tampered with in transit", backend)

    if not client.ping():
        log.warning("Could not reach backend on startup — will retry each heartbeat")

    _refresh_tool_definitions(backend)

    last_ai_check        = 0.0
    last_ai_sent         = 0.0
    last_extension_check = 0.0
    last_claude_check    = 0.0
    last_discover_check  = 0.0
    last_tool_def_check  = time.monotonic()
    last_standup_check   = 0.0
    last_ai_tools:     list[str] = []
    discovered_tools:  list[str] = []
    last_extensions:   list[str] = []
    known_shas:        dict[str, str | None] = {}
    last_repo_scan     = 0.0
    last_standup_date: str | None = None

    while not (stop_event and stop_event.is_set()):
        try:
            now = time.monotonic()
            app, proc_cwd = get_active_info()
            idle          = get_idle_seconds() > 120
            repo, branch  = find_git_repo(proc_cwd)

            ok = client.heartbeat(app, repo, branch, idle)
            if on_status:
                on_status(ok)
            log.debug("heartbeat ok=%s app=%s repo=%s idle=%s", ok, app, repo, idle)

            fg_root = _repo_root(proc_cwd)
            if fg_root:
                known_shas.setdefault(str(fg_root), None)
            if now - last_repo_scan >= REPO_SCAN_INTERVAL:
                for r in discover_repos():
                    known_shas.setdefault(str(r), None)
                last_repo_scan = now
                if len(known_shas) > _MAX_WATCHED_REPOS:
                    for k in list(known_shas)[:-_MAX_WATCHED_REPOS]:
                        known_shas.pop(k, None)

            for rp_str in list(known_shas):
                rp = Path(rp_str)
                r_name, r_branch = find_git_repo(rp)
                if not r_name:
                    known_shas.pop(rp_str, None)
                    continue
                for c in get_new_commits(rp, known_shas.get(rp_str)):
                    client.commit(r_name, r_branch, c)
                    log.info("commit %s %s", c["sha"], c["message"][:60])
                try:
                    head = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=rp, capture_output=True, text=True, timeout=3, **_NO_WINDOW,
                    )
                    if head.returncode == 0:
                        known_shas[rp_str] = head.stdout.strip()
                except Exception:
                    pass

            if now - last_extension_check >= EXTENSION_INTERVAL:
                exts = get_vscode_extensions()
                if exts != last_extensions:
                    client.vscode_extensions(exts)
                    log.info("Extensions: %d synced", len(exts))
                    last_extensions = exts
                last_extension_check = now

            if now - last_discover_check >= DISCOVER_INTERVAL:
                discovered_tools = discover_local_ai_tools()
                last_discover_check = now

            if now - last_ai_check >= AI_CHECK_INTERVAL:
                tools = sorted(set(detect_ai_tools(last_extensions)) | set(discovered_tools))
                if tools != last_ai_tools or now - last_ai_sent >= AI_RESEND_INTERVAL:
                    client.ai_event(tools)
                    log.info("AI tools: %s", tools or "none")
                    last_ai_tools = tools
                    last_ai_sent  = now
                last_ai_check = now

            if now - last_tool_def_check >= 21600:
                _refresh_tool_definitions(backend)
                last_tool_def_check = now

            if now - last_claude_check >= CLAUDE_INTERVAL:
                entries = get_claude_usage()
                if entries:
                    client.claude_usage(entries)
                    total = sum(e["input_tokens"] + e["output_tokens"] for e in entries)
                    log.info("Claude: %d entry(s) %s tokens", len(entries), f"{total:,}")
                last_claude_check = now

            if on_notify and now - last_standup_check >= STANDUP_INTERVAL:
                standup = client.get_pending_standup()
                if _should_notify(standup, last_standup_date):
                    on_notify("Your standup is ready", standup["text"])
                    client.ack_standup(standup["date"])
                    last_standup_date = standup["date"]
                    log.info("Standup delivered for %s", standup["date"])
                last_standup_check = now

        except Exception as e:
            log.error("Loop error: %s", e)

        for _ in range(HEARTBEAT_INTERVAL):
            if stop_event and stop_event.is_set():
                break
            time.sleep(1)


if __name__ == "__main__":
    token = load_token()
    if not token:
        print("No device token found. Run the desktop app to sign in first.")
        sys.exit(1)
    run(token, load_backend())
