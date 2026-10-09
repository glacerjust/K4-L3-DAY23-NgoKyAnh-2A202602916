"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n: int, name: str, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK [Bước {n}: {name}]", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực hiện 7 bước của runbook."""
    # Bước 1: 1 xac_nhan_outage
    prim_alive = False
    try:
        r = httpx.get(f"{URL[primary]}/healthz", timeout=1.5)
        prim_alive = (r.status_code == 200)
    except Exception:
        prim_alive = False

    t_step1 = step(1, "xac_nhan_outage", primary=primary, primary_alive=prim_alive, target=target)

    # Bước 2: 2 thong_bao_incident
    chaos_file = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos_file.exists():
        kills = [
            json.loads(line)
            for line in chaos_file.read_text().splitlines()
            if line.strip()
        ]
        kill_events = [k for k in kills if k.get("action") == "kill"]
        if kill_events:
            t_outage = kill_events[-1].get("ts")

    operator_ts = time.time()
    notification_delay_s = round(operator_ts - t_outage, 2) if t_outage else None

    if not confirm(auto, f"Outage phát hiện trên region-{primary}. Xác nhận failover sang region-{target}?"):
        step(2, "thong_bao_incident", status="rejected_by_operator")
        return {"ok": False, "aborted": True, "reason": "Operator declined failover"}

    step(
        2,
        "thong_bao_incident",
        t_outage=t_outage,
        operator_alert_ts=operator_ts,
        notification_delay_s=notification_delay_s,
    )

    # Bước 3: 3 scale_gpu_pool (gọi failover.failover DUY NHẤT 1 lần)
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_result=fo_res)
    if not fo_res.get("ok"):
        return {"ok": False, "step": 3, "failover": fo_res}

    # Bước 4: 4 verify_state_replica (chỉ đọc kết quả từ fo_res và state_of)
    target_state = fo.state_of(target)
    step(
        4,
        "verify_state_replica",
        target=target,
        target_state=target_state,
        rpo_seconds=fo_res.get("rpo_seconds"),
        docs_lost=fo_res.get("docs_lost"),
        embed_model_version=fo_res.get("embed_model_version"),
    )

    # Bước 5: 5 dns_cutover (xác nhận trạng thái cutover)
    active_path = pathlib.Path("edge/active_region")
    active_now = active_path.read_text().strip() if active_path.exists() else "unknown"
    cutover_ok = (active_now == target)
    step(5, "dns_cutover", active_region=active_now, cutover_ok=cutover_ok)

    # Bước 6: 6 verify_golden_signals (10 request thật vào region phụ)
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for _ in range(10):
            t_req = time.time()
            try:
                r = client.get(f"{URL[target]}/v1/infer", params={"q": "golden_signal_test"})
                lat_ms = round((time.time() - t_req) * 1000, 1)
                latencies.append(lat_ms)
                if r.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
                latencies.append(3000.0)
            time.sleep(0.05)

    latencies.sort()
    idx_p95 = min(len(latencies) - 1, int(len(latencies) * 0.95))
    p95_ms = latencies[idx_p95] if latencies else None
    error_rate = round(errors / 10.0, 2)
    step(
        6,
        "verify_golden_signals",
        p95_latency_ms=p95_ms,
        error_rate=error_rate,
        total_requests=10,
        errors=errors,
    )

    # Bước 7: 7 post_incident
    elapsed_total_s = round(time.time() - operator_ts, 2)
    step(
        7,
        "post_incident",
        elapsed_s=elapsed_total_s,
        cmd_measure="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300",
    )

    return {
        "ok": True,
        "target": target,
        "rpo_seconds": fo_res.get("rpo_seconds"),
        "docs_lost": fo_res.get("docs_lost"),
        "p95_ms": p95_ms,
        "error_rate": error_rate,
        "elapsed_s": elapsed_total_s,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
