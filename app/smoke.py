"""Live acceptance probe: create, process and poll a few requests to a terminal state.

Usage: python app/smoke.py --base-url http://localhost:5000 --timeout 120

Exits non-zero on HTTP contract violations or requests still unresolved at the
deadline. ``failed`` outcomes are reported, not hidden: the mock AI refuses or
omits fields on purpose, so not every sample is expected to be sent.
"""

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

DEFAULT_INPUTS = [
    "Enviar email a juan@example.com diciendo que su paquete está en camino.",
    "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.",
    "Manda un correo a marta@app.com: Tienes un nuevo mensaje.",
    "Recordatorio por teléfono al 699888777: cita a las 10:00",
    "Manda un mail a feda@test.com diciendo hola",
]
STATUSES = {"queued", "processing", "sent", "failed"}


@dataclass
class Report:
    requests: int = 0
    counts: dict = field(default_factory=dict)
    contract_errors: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    seconds_to_terminal: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.contract_errors and not self.unresolved

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "requests": self.requests,
                "terminal_counts": self.counts,
                "contract_errors": self.contract_errors,
                "unresolved": self.unresolved,
                "max_seconds_to_terminal": max(self.seconds_to_terminal, default=None),
            },
            indent=2,
        )


def run_smoke(client: httpx.Client, inputs, timeout: float, poll_interval: float = 1.0, clock=time.monotonic, sleep=time.sleep) -> Report:
    report = Report(requests=len(inputs))
    started, pending = clock(), {}
    for text in inputs:
        created = client.post("/v1/requests", json={"user_input": text})
        if created.status_code != 201 or not isinstance(created.json().get("id"), str):
            report.contract_errors.append(f"create returned {created.status_code}")
            continue
        rid = created.json()["id"]
        processed = client.post(f"/v1/requests/{rid}/process")
        if processed.status_code not in (200, 202):
            report.contract_errors.append(f"process {rid} returned {processed.status_code}")
            continue
        pending[rid] = clock()

    counts = Counter()
    deadline = started + timeout
    while pending and clock() < deadline:
        for rid in list(pending):
            res = client.get(f"/v1/requests/{rid}")
            body = res.json() if res.status_code == 200 else {}
            if res.status_code != 200 or body.get("id") != rid or body.get("status") not in STATUSES:
                report.contract_errors.append(f"status {rid} returned {res.status_code} {body.get('status')!r}")
                del pending[rid]
            elif body["status"] in ("sent", "failed"):
                counts[body["status"]] += 1
                report.seconds_to_terminal.append(round(clock() - pending.pop(rid), 2))
        if pending:
            sleep(poll_interval)

    report.counts = dict(counts)
    report.unresolved = sorted(pending)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:5000")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        report = run_smoke(client, DEFAULT_INPUTS, args.timeout, args.poll_interval)
    print(report.to_json())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
