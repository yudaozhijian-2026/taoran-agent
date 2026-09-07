"""The same front request must produce byte-identical model prompts to baseline."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from test_post_policy import visit


def test_front_prompt_matches_exact_production_baseline():
    root = Path(__file__).resolve().parents[1]
    code = '''
import json,sys
from taoran_agent.config import Settings
from taoran_agent.models import VisitDraftInput
from taoran_agent.knowledge import load_taoran_knowledge_snapshot
from taoran_agent.llm import ChatModelReviewer
v=VisitDraftInput.model_validate_json(sys.stdin.read())
r=ChatModelReviewer(Settings(_env_file=None),load_taoran_knowledge_snapshot())
try:
 print(json.dumps(r._messages(r._input(v,precheck=True),True),ensure_ascii=False,sort_keys=True))
finally:
 r.close()
'''
    # Captured from clean origin/main 0e510b4, using the same synthetic visit.
    result = subprocess.run(
        [sys.executable, "-c", code], input=visit().model_dump_json(),
        text=True, capture_output=True, check=True,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    ).stdout
    assert hashlib.sha256(result.encode()).hexdigest() == "d04674d53fc42425bfa2aa732d57eda9966a763d42eb9ebb27620808f06d9604"
