#!/usr/bin/env python3
"""Dependency-free local review UI for suspicious OCR words and table cells."""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from verification_pipeline import latest_decisions, read_jsonl


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OCR verification</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f4f1ea;color:#1c2430}.wrap{max-width:900px;margin:30px auto;padding:0 20px}
.card{background:white;border:1px solid #d8d1c3;border-radius:14px;padding:24px;box-shadow:0 8px 30px #29313d18}
.meta{display:flex;gap:14px;flex-wrap:wrap;color:#596273}.crop{display:flex;justify-content:center;background:#262b31;border-radius:9px;padding:20px;margin:18px 0;min-height:100px}
.crop img{max-width:100%;max-height:330px;object-fit:contain;image-rendering:auto}.values{display:grid;grid-template-columns:1fr 1fr;gap:12px}.value{background:#f6f7f8;padding:12px;border-radius:8px}
.label{font-size:12px;text-transform:uppercase;color:#68717e}.text{font-size:21px;word-break:break-word}.reasons{margin:12px 0}.badge{display:inline-block;background:#ffe7c2;color:#6d4100;padding:4px 8px;border-radius:99px;margin:3px;font-size:12px}
input{box-sizing:border-box;width:100%;font-size:20px;padding:12px;border:1px solid #aeb6c0;border-radius:8px;margin:12px 0}.actions{display:flex;gap:10px}.actions button{border:0;border-radius:8px;padding:11px 18px;font-size:16px;cursor:pointer}
.accept{background:#176b46;color:white}.correct{background:#245ca6;color:white}.skip{background:#d8dde4}.progress{margin:10px 0;color:#596273}.done{text-align:center;padding:50px 10px} @media(max-width:650px){.values{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><h1>OCR verification</h1><div id="progress" class="progress"></div><div id="card" class="card"></div></div>
<script>
let current=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function next(){const r=await fetch('/api/next');const data=await r.json();document.getElementById('progress').textContent=`Reviewed ${data.reviewed} of ${data.total}`;
 if(!data.item){document.getElementById('card').innerHTML='<div class="done"><h2>Review queue complete</h2><p>Run build-ground-truth, then evaluate.</p></div>';return}
 current=data.item;const pos=current.type==='table_cell'?` · row ${current.row}, column ${current.column}`:'';
 document.getElementById('card').innerHTML=`<div class="meta"><b>Page ${current.page}</b><span>${esc(current.type)}${pos}</span><span>confidence ${(100*current.confidence).toFixed(1)}%</span></div>
 <div class="crop"><img src="/crop/${encodeURI(current.crop)}" alt="OCR crop"></div><div class="values"><div class="value"><div class="label">Raw OCR</div><div class="text">${esc(current.raw_ocr)||'<i>empty</i>'}</div></div>
 <div class="value"><div class="label">Alternative OCR · ${(100*current.alternative_confidence).toFixed(1)}%</div><div class="text">${esc(current.alternative_ocr)||'<i>empty</i>'}</div></div></div>
 <div class="reasons">${current.flag_reasons.map(x=>`<span class="badge">${esc(x)}</span>`).join('')}</div><label class="label" for="correction">Correct value</label>
 <input id="correction" value="${esc(current.normalized_value||current.raw_ocr)}"><div class="actions"><button class="accept" onclick="decide('accept')">Accept (A)</button><button class="correct" onclick="decide('correct')">Correct (C)</button><button class="skip" onclick="decide('skip')">Skip (S)</button></div>`;}
async function decide(action){let corrected=document.getElementById('correction')?.value??'';if(action==='correct'&&!corrected.trim()){alert('Enter a corrected value.');return}
 await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:current.id,action,corrected_text:corrected})});next();}
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT')return;if(e.key.toLowerCase()==='a')decide('accept');if(e.key.toLowerCase()==='c')document.getElementById('correction')?.focus();if(e.key.toLowerCase()==='s')decide('skip')});next();
</script></body></html>"""


class ReviewState:
    def __init__(self, verification_dir: Path):
        self.root = verification_dir.resolve()
        self.queue = read_jsonl(self.root / "review" / "queue.jsonl")
        self.decisions_path = self.root / "review" / "decisions.jsonl"
        queue_by_id = {item["id"]: item for item in self.queue}
        self.decisions = {
            item_id: decision
            for item_id, decision in latest_decisions(self.decisions_path).items()
            if item_id in queue_by_id
            and (
                queue_by_id[item_id]["type"] != "table_cell"
                or decision.get("record_version") == queue_by_id[item_id].get("record_version")
            )
        }
        self.lock = threading.Lock()

    def next_item(self):
        return next((item for item in self.queue if item["id"] not in self.decisions), None)

    def save(self, payload):
        item = next((item for item in self.queue if item["id"] == payload.get("id")), None)
        action = payload.get("action")
        if not item or action not in {"accept", "correct", "skip"}:
            raise ValueError("Invalid review decision")
        corrected = str(payload.get("corrected_text", "")).strip()
        if action == "correct" and not corrected:
            raise ValueError("Correct requires corrected_text")
        decision = {
            "id": item["id"], "action": action,
            "record_version": item.get("record_version"),
            "verified_value": item["raw_ocr"] if action == "accept" else corrected if action == "correct" else None,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            with self.decisions_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(decision, ensure_ascii=False) + "\n")
            self.decisions[item["id"]] = decision
        return decision


def handler_factory(state: ReviewState):
    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, body: bytes, content_type: str, status=HTTPStatus.OK):
            self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self.send_bytes(HTML.encode(), "text/html; charset=utf-8")
            if path == "/api/next":
                item = state.next_item()
                payload = {"item": item, "reviewed": len(state.decisions), "total": len(state.queue)}
                return self.send_bytes(json.dumps(payload, ensure_ascii=False).encode(), "application/json")
            if path.startswith("/crop/"):
                relative = Path(unquote(path[len("/crop/"):]))
                target = (state.root / relative).resolve()
                if state.root not in target.parents or not target.is_file():
                    return self.send_bytes(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)
                return self.send_bytes(target.read_bytes(), "image/png")
            self.send_bytes(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if urlparse(self.path).path != "/api/decision":
                return self.send_bytes(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                decision = state.save(json.loads(self.rfile.read(length)))
                self.send_bytes(json.dumps(decision, ensure_ascii=False).encode(), "application/json")
            except (ValueError, json.JSONDecodeError) as error:
                self.send_bytes(json.dumps({"error": str(error)}).encode(), "application/json", HTTPStatus.BAD_REQUEST)

        def log_message(self, format, *args):
            return
    return Handler


def main():
    parser = argparse.ArgumentParser(description="Review suspicious OCR items in a local browser.")
    parser.add_argument("--source", type=Path, default=Path("output/scanned_report"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    state = ReviewState(args.source.resolve() / "verification")
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(state))
    print(f"Review {len(state.queue) - len(state.decisions)} remaining items at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
