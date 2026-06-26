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

# ── Auto-install minimal deps ─────────────────────────────────────────────────

def _ensure_deps():
    for pkg in ("psutil", "requests", "keyring"):
        try:
            __import__(pkg)
        except ImportError:
            log.info("Installing %s…", pkg)
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg],
                stdout=subprocess.DEVNULL,
            )

_ensure_deps()

import psutil    # noqa: E402
import requests  # noqa: E402
import keyring   # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

KEYRING_SERVICE   = "da-agent"
KEYRING_TOKEN_KEY = "device-token"
KEYRING_URL_KEY   = "backend-url"

DEFAULT_BACKEND = os.environ.get("DA_BACKEND", "http://localhost:8000")

HEARTBEAT_INTERVAL  = 30   # seconds
AI_CHECK_INTERVAL   = 60
EXTENSION_INTERVAL  = 600
CLAUDE_INTERVAL     = 300

# Known process name keywords → tool name
AI_PROC_MAP: dict[str, str] = {
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
}

# VS Code extension publisher.name → tool name
VSCODE_EXT_MAP: dict[str, str] = {
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

# Keywords that flag an unknown process as likely AI-related
_AI_KEYWORDS = {"gpt", "llm", "coder", "codex", "assistant", "autocomplete", "intellicode"}

# ── Config (OS keyring) ───────────────────────────────────────────────────────

def load_token() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)

def load_backend() -> str:
    return keyring.get_password(KEYRING_SERVICE, KEYRING_URL_KEY) or DEFAULT_BACKEND

def save_credentials(token: str, backend: str) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY, token)
    keyring.set_password(KEYRING_SERVICE, KEYRING_URL_KEY, backend)
    log.info("Credentials saved to OS keyring")

# ── Active window / git repo ──────────────────────────────────────────────────

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


def find_git_repo(path: Path | None) -> tuple[str | None, str | None]:
    if not path:
        return None, None
    for p in [path, *path.parents]:
        git_dir = p / ".git"
        if git_dir.is_dir():
            branch = "unknown"
            try:
                head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
                if head.startswith("ref: refs/heads/"):
                    branch = head[len("ref: refs/heads/"):]
            except Exception:
                pass
            return p.name, branch
    return None, None

# ── Local git commits ─────────────────────────────────────────────────────────

def get_new_commits(repo_path: Path, since_sha: str | None) -> list[dict]:
    try:
        if since_sha:
            cmd = ["git", "log", f"{since_sha}..HEAD", "--format=%H|%s|%aI", "--no-merges"]
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            cmd   = ["git", "log", f"--after={today} 00:00", "--format=%H|%s|%aI", "--no-merges"]

        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=5)
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
                cwd=repo_path, capture_output=True, text=True, timeout=5,
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

# ── AI tool detection ─────────────────────────────────────────────────────────

def detect_ai_tools(installed_extensions: list[str] | None = None) -> list[str]:
    detected: set[str] = set()

    # 1. Process scan — known tools + unknown heuristic
    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (proc.info["name"] or "").lower().replace(".exe", "")
                # Known map
                for keyword, tool in AI_PROC_MAP.items():
                    if keyword in name:
                        detected.add(tool)
                        break
                else:
                    # Unknown process — flag if name contains an AI keyword
                    if any(kw in name for kw in _AI_KEYWORDS):
                        detected.add(name)  # surface raw name
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    # 2. VS Code extensions — catches installed tools even if not running as a process
    for ext_id in (installed_extensions or []):
        tool = VSCODE_EXT_MAP.get(ext_id.lower())
        if tool:
            detected.add(tool)

    return sorted(detected)

# ── Claude Code JSONL usage ───────────────────────────────────────────────────

def _repo_from_jsonl(path: Path) -> str:
    """Decode repo name from Claude's encoded project directory name.
    e.g. 'e--AI' -> 'AI', '-home-user-projects-myrepo' -> 'myrepo'
    """
    parts = [p for p in re.split(r"-+", path.parent.name) if p]
    return parts[-1] if parts else path.parent.name


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


def get_claude_usage(file_offsets: dict[str, int]) -> list[dict]:
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []

    # key: (date, model, repo)
    agg: dict[tuple[str, str, str], dict] = {}

    for jsonl_file in claude_dir.rglob("*.jsonl"):
        file_key    = str(jsonl_file)
        last_offset = file_offsets.get(file_key, 0)
        repo        = _repo_from_jsonl(jsonl_file)
        try:
            if jsonl_file.stat().st_size <= last_offset:
                continue
            with jsonl_file.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(last_offset)
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
                        dt       = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        date_str = dt.astimezone().strftime("%Y-%m-%d")
                    except Exception:
                        date_str = datetime.now().strftime("%Y-%m-%d")

                    model = (msg.get("model") or "claude-sonnet").lower()
                    key   = (date_str, model, repo)
                    if key not in agg:
                        agg[key] = {"input": 0, "cache_w": 0, "cache_r": 0,
                                    "output": 0, "messages": 0, "files": set()}
                    a = agg[key]
                    a["input"]    += usage.get("input_tokens", 0)
                    a["cache_w"]  += usage.get("cache_creation_input_tokens", 0)
                    a["cache_r"]  += usage.get("cache_read_input_tokens", 0)
                    a["output"]   += usage.get("output_tokens", 0)
                    a["messages"] += 1
                    for fp in _files_from_content(msg.get("content", [])):
                        a["files"].add(fp)
                file_offsets[file_key] = f.tell()
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
        }
        for (date, model, repo), v in agg.items() if v["messages"] > 0
    ]

# ── VS Code extensions ────────────────────────────────────────────────────────

def get_vscode_extensions() -> list[str]:
    for cmd in (["code", "--list-extensions"], ["cursor", "--list-extensions"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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

# ── HTTP client ───────────────────────────────────────────────────────────────

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

# ── Main loop ─────────────────────────────────────────────────────────────────

def run(token: str, backend: str, on_status=None):
    """
    Main collection loop. on_status(connected: bool) is called on tray icon
    to update the connection indicator.
    """
    client = AgentClient(token, backend)
    log.info("Agent starting — backend=%s", backend)

    if not client.ping():
        log.warning("Could not reach backend on startup — will retry each heartbeat")

    last_ai_check        = 0.0
    last_extension_check = 0.0
    last_claude_check    = 0.0
    last_ai_tools:   list[str] = []
    last_extensions: list[str] = []
    known_shas:      dict[str, str | None] = {}
    claude_offsets:  dict[str, int] = {}

    while True:
        try:
            now = time.monotonic()
            app, proc_cwd = get_active_info()
            idle          = get_idle_seconds() > 120
            repo, branch  = find_git_repo(proc_cwd)

            ok = client.heartbeat(app, repo, branch, idle)
            if on_status:
                on_status(ok)
            log.debug("heartbeat ok=%s app=%s repo=%s idle=%s", ok, app, repo, idle)

            # Commit polling
            if repo and branch and proc_cwd:
                repo_path: Path | None = None
                check = proc_cwd if (proc_cwd / ".git").is_dir() else None
                if not check:
                    for p in proc_cwd.parents:
                        if (p / ".git").is_dir():
                            repo_path = p
                            break
                else:
                    repo_path = check

                if repo_path:
                    last_sha   = known_shas.get(str(repo_path))
                    new_commits = get_new_commits(repo_path, last_sha)
                    for c in new_commits:
                        client.commit(repo, branch, c)
                        log.info("commit %s %s", c["sha"], c["message"][:60])
                    try:
                        head = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=repo_path, capture_output=True, text=True, timeout=3,
                        )
                        if head.returncode == 0:
                            known_shas[str(repo_path)] = head.stdout.strip()
                    except Exception:
                        pass

            # VS Code extensions (check before AI tools so extensions feed into detection)
            if now - last_extension_check >= EXTENSION_INTERVAL:
                exts = get_vscode_extensions()
                if exts != last_extensions:
                    client.vscode_extensions(exts)
                    log.info("Extensions: %d synced", len(exts))
                    last_extensions = exts
                last_extension_check = now

            # AI tools — process scan + installed extensions
            if now - last_ai_check >= AI_CHECK_INTERVAL:
                tools = detect_ai_tools(last_extensions)
                if tools != last_ai_tools:
                    client.ai_event(tools)
                    log.info("AI tools: %s", tools or "none")
                    last_ai_tools = tools
                last_ai_check = now

            # Claude usage
            if now - last_claude_check >= CLAUDE_INTERVAL:
                entries = get_claude_usage(claude_offsets)
                if entries:
                    client.claude_usage(entries)
                    total = sum(e["input_tokens"] + e["output_tokens"] for e in entries)
                    log.info("Claude: %d entry(s) %s tokens", len(entries), f"{total:,}")
                last_claude_check = now

        except Exception as e:
            log.error("Loop error: %s", e)

        time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
    token = load_token()
    if not token:
        print("No device token found. Run the desktop app to sign in first.")
        sys.exit(1)
    run(token, load_backend())
