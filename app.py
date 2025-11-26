import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Tuple, Union
from uuid import uuid4


BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "history.json"
STATIC_DIR = BASE_DIR / "public"


def entry_fingerprint(entry: dict) -> Tuple[str, str, Tuple[Tuple[str, str], ...], str]:
    headers = entry.get("headers") or {}
    normalized_headers = {str(k).strip(): str(v).strip() for k, v in headers.items()}
    headers_tuple = tuple(sorted(normalized_headers.items()))
    return (
        (entry.get("method") or "").upper(),
        (entry.get("url") or "").strip(),
        headers_tuple,
        entry.get("body") or "",
    )


def load_history() -> List[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as handle:
            entries = json.load(handle)
    except json.JSONDecodeError:
        return []
    unique: List[dict] = []
    seen = set()
    for entry in entries:
        fingerprint = entry_fingerprint(entry)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(entry)
    return unique


def save_history(history: List[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=2)


def remove_entry(entry_id: str) -> bool:
    history = load_history()
    new_history = [item for item in history if str(item.get("id")) != str(entry_id)]
    if len(new_history) == len(history):
        return False
    save_history(new_history)
    return True


def rename_entry(entry_id: str, name: str) -> bool:
    history = load_history()
    found = False
    for item in history:
        if str(item.get("id")) == str(entry_id):
            item["name"] = name
            found = True
            break
    if found:
        save_history(history)
    return found


def reorder_entries(order: List[str]) -> List[dict]:
    history = load_history()
    by_id = {str(item.get("id")): item for item in history}
    new_history: List[dict] = []
    seen = set()
    for entry_id in order:
        entry = by_id.get(str(entry_id))
        if entry and entry_id not in seen:
            new_history.append(entry)
            seen.add(entry_id)
    for entry in history:
        entry_id = str(entry.get("id"))
        if entry_id not in seen:
            new_history.append(entry)
            seen.add(entry_id)
    save_history(new_history)
    return new_history


def record_entry(entry: dict) -> dict:
    history = load_history()
    fingerprint = entry_fingerprint(entry)

    # Remove existing entry with the same request signature.
    existing_index = next((idx for idx, item in enumerate(history) if entry_fingerprint(item) == fingerprint), None)
    if existing_index is not None:
        previous = history[existing_index]
        merged = {**previous, **entry}
        # Preserve original id and name if not overwritten.
        merged["id"] = previous.get("id") or entry.get("id")
        if not merged.get("name") and previous.get("name"):
            merged["name"] = previous.get("name")
        history[existing_index] = merged
    else:
        history.append(entry)

    history = history[:100]
    save_history(history)
    return entry


def send_external_request(
    method: str, url: str, headers: Dict[str, str], body: Union[str, bytes, None]
) -> Tuple[int, Dict[str, str], str]:
    data: Union[bytes, None]
    if body:
        data = body if isinstance(body, bytes) else body.encode("utf-8")
    else:
        data = None

    request = urllib.request.Request(url, data=data, method=method.upper())
    for key, value in headers.items():
        request.add_header(key, value)

    charset = None

    try:
        with urllib.request.urlopen(request) as response:
            response_body = response.read()
            status_code = response.status
            response_headers = dict(response.headers.items())
            charset = response.headers.get_content_charset()
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        status_code = exc.code
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        if exc.headers:
            charset = exc.headers.get_content_charset()
    except urllib.error.URLError as exc:
        message = f"Request failed: {exc.reason}"
        return 599, {}, message

    decoded_body = response_body.decode(charset or "utf-8", errors="replace")
    return status_code, response_headers, decoded_body


class ApiHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(directory or STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:  # pragma: no cover
        return  # Silence default console noise

    def _set_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(content_length) if content_length else b""
        if not raw_body:
            return {}
        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._set_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self.send_response(200)
            self._set_cors()
            self.end_headers()
            return
        super().do_OPTIONS()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/api/history", "/api/commands"):
            history = load_history()
            self._write_json(200, {"history": history})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/send":
            payload = self._json_body()
            url = payload.get("url")
            method = (payload.get("method") or "GET").upper()
            headers = payload.get("headers") or {}
            body = payload.get("body") or ""
            name = payload.get("name") or ""

            if not url:
                self._write_json(400, {"error": "url is required"})
                return

            status_code, response_headers, response_body = send_external_request(method, url, headers, body)

            entry = {
                "id": str(uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "response_status": status_code,
                "name": name,
            }
            record_entry(entry)

            self._write_json(
                200,
                {
                    "status": status_code,
                    "response_headers": response_headers,
                    "response_body": response_body,
                    "saved": entry,
                },
            )
            return

        if self.path == "/api/resend":
            payload = self._json_body()
            target_id = payload.get("id")
            if not target_id:
                self._write_json(400, {"error": "id is required"})
                return

            history = load_history()
            entry = next((item for item in history if item.get("id") == target_id), None)
            if not entry:
                self._write_json(404, {"error": "request not found"})
                return

            status_code, response_headers, response_body = send_external_request(
                entry["method"], entry["url"], entry.get("headers", {}), entry.get("body", "")
            )

            entry["response_status"] = status_code
            entry["timestamp"] = datetime.now(timezone.utc).isoformat()
            record_entry(entry)

            self._write_json(
                200,
                {
                    "status": status_code,
                    "response_headers": response_headers,
                    "response_body": response_body,
                    "saved": entry,
                },
            )
            return

        if self.path == "/api/delete":
            payload = self._json_body()
            target_id = payload.get("id")
            if not target_id:
                self._write_json(400, {"error": "id is required"})
                return

            removed = remove_entry(str(target_id))
            if not removed:
                self._write_json(404, {"error": "request not found"})
                return

            self._write_json(200, {"deleted": target_id})
            return

        if self.path == "/api/rename":
            payload = self._json_body()
            target_id = payload.get("id")
            name = payload.get("name") or ""
            if not target_id:
                self._write_json(400, {"error": "id is required"})
                return
            renamed = rename_entry(str(target_id), str(name))
            if not renamed:
                self._write_json(404, {"error": "request not found"})
                return
            self._write_json(200, {"renamed": target_id, "name": name})
            return

        if self.path == "/api/reorder":
            payload = self._json_body()
            order = payload.get("order") or []
            if not isinstance(order, list):
                self._write_json(400, {"error": "order must be a list"})
                return
            reordered = reorder_entries([str(i) for i in order])
            self._write_json(200, {"history": reordered})
            return

        self._write_json(404, {"error": "not found"})


def run_server(port: int = 8000) -> None:
    handler = lambda *args, **kwargs: ApiHandler(*args, directory=STATIC_DIR, **kwargs)  # noqa: E731
    httpd = HTTPServer(("0.0.0.0", port), handler)
    print(f"Server running at http://localhost:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        httpd.server_close()


if __name__ == "__main__":
    run_server()
