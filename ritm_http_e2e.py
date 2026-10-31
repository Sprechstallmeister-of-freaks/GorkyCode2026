"""Ritm real HTTP end-to-end smoke tests, using a disposable SQLite DB.

Run from the project's root using the project's virtualenv:
    python ritm_http_e2e.py  # run using project virtualenv

Starts its OWN Uvicorn server on a free loopback port, and shuts it down.
No existing assistant.db file is touched. No third-party Python libs are needed.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PASSED = 0


def check(condition: bool, label: str, explanation: str = ""):
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {label}. {explanation}")
    PASSED += 1
    print(f"  [PASS {PASSED:02}] {label}", flush=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(base: str, method: str, endpoint: str, data=None, query=None):
    url = base + endpoint
    if query:
        url += "?" + urllib.parse.urlencode(query)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def run(base: str):
    day = (date.today() + timedelta(days=40)).isoformat()
    suffix = str(os.getpid())
    a_title = f"E2E Ritm primary {suffix}"
    b_title = f"E2E Ritm second {suffix}"

    code, health = http(base, "GET", "/api/health")
    check(code == 200 and health["status"] == "ok" and health["records"] == 0,
          "Health + isolated empty DB", str(health))
    check(health["llm_connected"] is False, "LLM not attached (expected)")

    code, spec = http(base, "GET", "/api/tools")
    names = [t["function"]["name"] for t in spec.get("tools", [])]
    check(code == 200 and set(names) == {"create_event", "list_events", "search_events", "get_event", "update_event", "cancel_event"},
          "6 static tools, function.name", str(names))
    check(all("parameters" in t["function"] for t in spec["tools"]), "JSON Schema for every tool")

    code, invalid = http(base, "POST", "/api/events", {"kind":"meeting", "title":"Invalid meeting"})
    check(code == 422, "Pydantic rejects meeting without date/time", str(invalid))
    code, invalid = http(base, "POST", "/api/events", {"kind":"task", "title":"bad", "sql":"DROP TABLE events"})
    check(code == 422, "Pydantic rejects unknown/SQL field")

    code, a = http(base, "POST", "/api/events", {
        "kind":"meeting", "title":a_title, "scheduled_date":day,
        "scheduled_time":"15:00", "duration_minutes":60,
    })
    check(code == 201 and a.get("status") == "open", "Create meeting A", str(a))
    aid = a["id"]

    code, got = http(base, "GET", f"/api/events/{aid}")
    check(code == 200 and got["title"] == a_title, "Read meeting by ID")

    code, listed = http(base, "GET", "/api/events", query={"from_date":day, "to_date":day})
    check(code == 200 and listed["total"] == 1, "List events for date", str(listed))

    code, searched = http(base, "GET", "/api/search", query={"q": a_title})
    check(code == 200 and any(e["id"] == aid for e in searched["items"]), "Search event via SQLite")

    code, conflict = http(base, "POST", "/api/events", {
        "kind":"meeting", "title":"Overlapping", "scheduled_date":day,
        "scheduled_time":"15:30", "duration_minutes":60,
    })
    check(code == 409, "Overlapping CREATE rejected", str(conflict))

    code, b = http(base, "POST", "/api/events", {
        "kind":"meeting", "title":b_title, "scheduled_date":day,
        "scheduled_time":"17:00", "duration_minutes":60,
    })
    check(code == 201, "Create non-overlapping meeting B", str(b))
    bid = b["id"]

    code, conflict = http(base, "PATCH", f"/api/events/{bid}", {"scheduled_time":"15:45", "expected_version":1})
    check(code == 409, "Overlapping UPDATE rejected", str(conflict))
    code, unchanged = http(base, "GET", f"/api/events/{bid}")
    check(code == 200 and unchanged["scheduled_time"][:5] == "17:00", "Conflict does not corrupt saved meeting")

    code, a2 = http(base, "PATCH", f"/api/events/{aid}", {"title":a_title+" updated", "expected_version":1})
    check(code == 200 and a2["version"] == 2, "Successful PATCH increments version", str(a2))
    code, conflict = http(base, "PATCH", f"/api/events/{aid}", {"title":"stale", "expected_version":1})
    check(code == 409, "Optimistic concurrency rejects stale version")

    code, denied = http(base, "DELETE", f"/api/events/{aid}")
    check(code == 409, "Direct DELETE prohibited")
    code, denied = http(base, "PATCH", f"/api/events/{aid}", {"status":"cancelled"})
    check(code == 409, "PATCH status=cancelled prohibited")

    sid_no = "e2e_no_"+suffix
    code, prompt = http(base, "POST", f"/api/events/{aid}/cancel-request", {"session_id":sid_no})
    check(code == 200 and prompt["action"] == "clarify", "Cancellation requires confirmation question", str(prompt))
    code, still = http(base, "GET", f"/api/events/{aid}")
    check(code == 200 and still["status"] == "open", "Event stays open before confirmation")
    code, declined = http(base, "POST", "/api/chat", {"session_id":sid_no, "message":"нет"})
    check(code == 200 and declined["action"] == "cancel_pending", "Explicit NO cancels pending action", str(declined))
    code, still = http(base, "GET", f"/api/events/{aid}")
    check(still["status"] == "open", "Event still open after NO")

    sid_yes = "e2e_yes_"+suffix
    code, prompt = http(base, "POST", f"/api/events/{aid}/cancel-request", {"session_id":sid_yes})
    check(code == 200 and prompt["action"] == "clarify", "Second, separate cancellation challenge")
    code, confused = http(base, "POST", "/api/chat", {"session_id":sid_yes, "message":"может быть"})
    check(code == 200 and confused["action"] == "clarify", "Ambiguous confirmation does NOT cancel")
    code, approved = http(base, "POST", "/api/chat", {"session_id":sid_yes, "message":"да"})
    check(code == 200 and approved["action"] == "cancel_event", "Explicit YES cancels event", str(approved))
    code, cancelled = http(base, "GET", f"/api/events/{aid}")
    check(code == 200 and cancelled["status"] == "cancelled", "Cancellation persisted in SQLite")

    code, active = http(base, "GET", "/api/events", query={"from_date":day,"to_date":day})
    code2, all_events = http(base, "GET", "/api/events", query={"from_date":day,"to_date":day,"include_cancelled":"true"})
    check(code == 200 and code2 == 200 and active["total"] == 1 and all_events["total"] == 2,
          "Soft-delete hidden by default, visible when requested")

    code, history = http(base, "GET", f"/api/sessions/{sid_yes}/history")
    check(code == 200 and len(history["messages"]) >= 4, "Dialogue history saved")

    # Test stale confirmation: version changes after challenge and before YES.
    sid_stale = "e2e_stale_"+suffix
    code, prompt = http(base, "POST", f"/api/events/{bid}/cancel-request", {"session_id":sid_stale})
    check(code == 200, "Create stale confirmation challenge")
    code, updated = http(base, "PATCH", f"/api/events/{bid}", {"title":b_title+" changed", "expected_version":1})
    check(code == 200 and updated["version"] == 2, "Update event after challenge")
    code, stale_result = http(base, "POST", "/api/chat", {"session_id":sid_stale, "message":"да"})
    check(code == 200 and stale_result["action"] == "conflict", "Stale confirmation rejected", str(stale_result))
    code, unchanged = http(base, "GET", f"/api/events/{bid}")
    check(code == 200 and unchanged["status"] == "open", "Stale confirmation preserves event")

    code, chat = http(base, "POST", "/api/chat", {"session_id":"e2e_rules_"+suffix, "message":"Что у меня сегодня?"})
    check(code == 200 and chat["needs_llm"] is False, "Rule-based chat handles known intent", str(chat))

    print(f"\nSUCCESS: {PASSED} HTTP end-to-end checks passed.")
    print("Existing assistant.db not accessed. Test database deleted with temporary directory.")


def main():
    if not (ROOT / "assistant" / "api.py").exists():
        raise SystemExit("Place ritm_http_e2e.py in the Ritm project root (beside run.py).")
    with tempfile.TemporaryDirectory(prefix="ritm_http_e2e_") as scratch:
        db = Path(scratch) / "isolated.db"
        logfile = Path(scratch) / "uvicorn.log"
        env = os.environ.copy()
        env.update({
            "ASSISTANT_DB": str(db),
            "ASSISTANT_IMPORT_SEED": "0",
            "ASSISTANT_TIMEZONE": "Europe/Moscow",
            "PYTHONIOENCODING": "utf-8",
        })
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        print(f"Starting isolated HTTP server at {base}")
        print(f"Temporary DB: {db}")
        with logfile.open("w", encoding="utf-8") as output:
            process = subprocess.Popen([
                sys.executable, "-m", "uvicorn", "assistant.api:app",
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
            ], cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Uvicorn exited during startup")
                    try:
                        status, _ = http(base, "GET", "/api/health")
                        if status == 200:
                            break
                    except (urllib.error.URLError, TimeoutError, ConnectionError):
                        time.sleep(0.15)
                else:
                    raise RuntimeError("Uvicorn did not start within 15 seconds")
                run(base)
            except Exception:
                print("\nServer log:\n" + logfile.read_text(encoding="utf-8", errors="replace")[-6000:], file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)
