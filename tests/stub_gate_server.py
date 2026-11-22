"""Local demo WITHOUT a TypeSafe key: replaces the Jev gate with a keyword stand-in.

This is NOT Jev and proves nothing about the real gate. It only lets you click through the
flows (rule replies, the local Qwen fallback, confirmations, handoffs) before you have a key.

    .venv/bin/python tests/stub_gate_server.py      then open http://localhost:7865
"""
import re
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import jev  # noqa: E402
from app.main import app  # noqa: E402

# What the customer asked about -> words a reply must contain to count as covering it.
TOPICS = {
    r"cancel": r"cancel",
    r"return|send it back": r"return",
    r"refund|money": r"refund",
    r"arriv|deliver|where is|track|when will": r"expected|deliver|arriv|shipped|transit",
}
ESCALATE = (
    r"torn|damaged|broken|defective|wrong item|charged twice|chargeback|lawyer|legal|unsafe"
    r"|(speak|talk) to (a |an )?(person|human|agent)"
)


def stand_in_score(message, history, item, policy, today, reply):
    missed = [
        asked
        for asked, covered in TOPICS.items()
        if re.search(asked, message, re.I) and not re.search(covered, reply, re.I)
    ]
    return {
        "supported": 0.95,  # a keyword stub cannot judge support; only the real gate does
        "complete": 0.2 if missed else 0.95,
        "requires_human": 0.9 if re.search(ESCALATE, message, re.I) else 0.02,
        "model": "stand-in",
        "input_tokens": None,
        "cost_usd": None,
        "request_id": None,
        "elapsed_ms": 0,
    }


jev.score = stand_in_score
jev.GATE["status"] = "STAND-IN GATE: keyword stub, not Jev (no TypeSafe key)"

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=7865)
