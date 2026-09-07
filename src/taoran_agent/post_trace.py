"""Private per-attempt stream evidence, safe to snapshot while HTTP is running."""
import json
import os
from pathlib import Path
from threading import Lock
from time import monotonic
from uuid import uuid4


class PostStreamTrace:
    def __init__(self, settings):
        self.lock = Lock()
        self.started = monotonic()
        self.last_saved = 0.0
        self.closed = False
        self.state = {"model_first_byte_ms": None, "model_complete_ms": None,
                      "model_request_id": None, "phase": "awaiting_response",
                      "generated_text": "", "generated_text_truncated": False}
        self.evidence_id = "stream-" + uuid4().hex
        self.path = Path(settings.database_path).resolve().parent / "model-failure-evidence" / (self.evidence_id + ".json")
        self.save_error = False

    def update(self, *, text="", **values):
        with self.lock:
            if self.closed:
                return
            self.state.update(values)
            if text:
                remaining = 131072 - len(self.state["generated_text"])
                self.state["generated_text"] += text[:remaining]
                self.state["generated_text_truncated"] |= len(text) > remaining
            now = monotonic()
            if now - self.last_saved >= 1 or values.get("model_complete_ms") is not None or values.get("model_first_byte_ms") is not None or "model_request_id" in values:
                self._save()
                self.last_saved = now

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            content = json.dumps(self.state, ensure_ascii=False)
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
                file.write(content)
                file.flush()
            os.replace(temporary, self.path)
            self.save_error = False
        except OSError:
            self.save_error = True

    def finish(self):
        with self.lock:
            self.closed = True
            self.state["observed_ms"] = int((monotonic() - self.started) * 1000)
            self._save()
            return dict(self.state)
