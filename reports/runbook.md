# Runbook 1 trang — Region chính down

Runbook dành cho on-call kỹ sư thực thi trong tình huống khẩn cấp (3 AM). Mỗi bước đều có lệnh copy-paste trực tiếp, tín hiệu hoàn thành rõ ràng, phân công trách nhiệm và cơ chế rollback an toàn.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | `a.alive=false` hoặc 3 lần probe liên tiếp trả lỗi/timeout | SRE On-call |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs` | Timestamp mở incident được ghi vào `reports/runbook-run.jsonl`, kênh sự cố được kích hoạt | Incident Commander / On-call |
| 3 | Restore state ở region phụ | `python state/snapshot.py get --region b --backend fs` | JSON trả về có `restored_at`, `vectors.sqlite` và model weights đã được khôi phục tại `state/region-b/` | Data Platform / SRE |
| 4 | Scale pool warm→full | `echo full > state/region-b/pool_state` | `curl -s http://127.0.0.1:8002/readyz` trả HTTP 200 `{"ready": true}` sau thời gian warmup | ML Platform On-call |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl -s http://127.0.0.1:8080/edge/state` hiển thị `"active_region": "b"` | Traffic / Network Engineer |
| 6 | Verify golden signals | `for i in {1..10}; do curl -s http://127.0.0.1:8080/v1/infer; done` | 10/10 request trả HTTP 200 từ `served_by: b`, p95 latency < 200ms, error rate = 0% | SRE On-call |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Log hiển thị `rto_verdict: PASS` (RTO ≤ 300s), xuất kết quả phân tích số liệu ra report | SRE Lead |

---

### Cơ chế Rollback (Failover ngược về Region A)

**Điều kiện tiên quyết để trả traffic về Region A:**
1. **Ổn định hệ thống (Stability Period):** Region A đã phục hồi, probe `/readyz` trả về HTTP 200 ổn định liên tục tối thiểu **15 phút** mà không có bất kỳ hiện tượng flapping nào.
2. **Đồng bộ dữ liệu ngược (Reverse Data Sync):** Toàn bộ dữ liệu/document mới được ghi vào Region B trong khoảng thời gian diễn ra sự cố phải được replicate ngược về Region A (`rpo_seconds = 0`, `docs_lost = 0`).
3. **Cấm tuyệt đối Full-Auto Failback:** Không thiết lập cơ chế chuyển vùng tự động ngược lại (Anti-Pattern: thiếu circuit breaker sẽ dẫn đến hiện tượng 2 region flap qua lại liên tục, làm nghẽn mạng và sập cả 2 vùng).

**Thẩm quyền quyết định (Authority):**
- Chỉ **Incident Commander (IC)** phối hợp với **Tech Lead/VP Engineering** mới có quyền phê duyệt thực hiện failback sau khi đã kiểm tra hoàn tất checklist kiểm định.
