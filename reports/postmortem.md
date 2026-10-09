# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T04:17:25 | Outage bắt đầu (kill Region A) | `chaos/chaos-events.jsonl:5` |
| 2026-10-09T04:17:27 | User đầu tiên bị ảnh hưởng (HTTP 503 ReadTimeout) | `reports/drill-2-withdr.jsonl:27` |
| 2026-10-09T04:17:40 | Health check alert (Region A UNHEALTHY sau 3 lần fail) | `reports/health-events.jsonl:1` |
| 2026-10-09T04:17:46 | Operator confirm cutover & DNS cutover hoàn tất | `reports/failover-events.jsonl:5` |
| 2026-10-09T04:17:50 | Resolved (request đầu tiên OK từ Region B) | `reports/drill-2-withdr.jsonl:37` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `25.3s` · gap: `-274.7s` (đạt sớm hơn mục tiêu 274.7s)
- RPO mục tiêu: 300s · đo được: `8.01s` (`4` doc bị mất) · gap: `-291.99s`
- **Bước tốn nhiều giây nhất:** `Health-check detection floor (14.9s)` — vì cơ chế chống flapping yêu cầu 3 lần probe liên tiếp thất bại với chu kỳ 5 giây (`interval_s × threshold = 5.0 × 3 = 15.0s`), chiếm tới 59.3% tổng thời gian RTO.

## 3. Root cause (5 whys)

1. *Tại sao user gặp lỗi timeout?* Do Region A bị netblock (mô phỏng mất kết nối mạng hoàn toàn).
2. *Tại sao không failover tức thì ở giây đầu tiên?* Vì cần sàn thời gian phát hiện (15s) để xác nhận outage thật sự, tránh báo động giả do mạng chập chờn ngắn hạn.
3. *Tại sao Region B không thể nhận traffic ngay lập tức?* Vì Region B khởi đầu rỗng (active-passive), cần khôi phục vector DB, nạp model weights và warm up GPU pool.
4. *Nếu đây là outage thật, bước nào trong runbook dễ thất bại nhất?* Bước `2_restore_snapshot` và `4_wait_ready`. Trong môi trường sản xuất thật, model weights lên đến hàng chục GB, nếu tải qua mạng WAN liên vùng khi gặp nghẽn băng thông thì RTO sẽ vượt ngưỡng 300s.
5. *Tại sao quy trình runbook vẫn duy trì bán tự động (semi-automated)?* Nhằm đảm bảo có sự xác nhận của kỹ sư on-call trước khi chuyển DNS, ngăn chặn hiện tượng flap 2 chiều liên tục (split-brain) khi sự cố chỉ mang tính cục bộ.

## 4. Action items (có owner + deadline)

| # | Action item | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Duy trì GPU pool ở trạng thái warm liên tục và pre-cache model weights tại Region B | ML Platform | 2026-10-20 | Giảm GPU warm-up và download time về 0 giây |
| 2 | Nâng cấp cơ chế đồng bộ vector DB sang CDC streaming (thay vì snapshot định kỳ 30s) | Data Platform | 2026-10-25 | Giảm RPO từ 8.01s xuống dưới 1s (mất 0-1 docs) |
| 3 | Tối ưu DNS TTL xuống 2 giây và tích hợp Geo-DNS Anycast failover | SRE / Traffic | 2026-11-01 | Giảm độ trễ DNS cache propagation thêm 2.2 giây |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - Giá trị là: `5.0s × 3 = 15.0 giây`.
   - Nó chiếm xấp xỉ **59.3%** tổng RTO đo được (`15.0s / 25.3s`). Đây là thành phần chiếm tỷ trọng lớn nhất trong toàn bộ thời gian gián đoạn dịch vụ.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - Nếu hạ interval xuống 1s (với threshold = 3), sàn phát hiện giảm từ 15s xuống 3s, tức là **RTO sẽ giảm 12 giây**.
   - Cái giá phải trả là **nguy cơ Flapping cực kỳ nghiêm trọng**: Chỉ một dao động mạng ngắn (micro-outage hoặc network jitter 3 giây) cũng sẽ kích hoạt toàn bộ quy trình failover sang Region B. Khi Region A vừa ổn định lại, hệ thống có thể bị cuốn vào vòng lặp failover qua lại liên tục giữa 2 region, làm nghẽn hạ tầng và gây gián đoạn dịch vụ trầm trọng hơn bản thân sự cố ban đầu.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - Con số `docs_lost = 4` có nghĩa là toàn bộ 4 tài liệu/giao dịch được ingest trong khoảng 8.01 giây trước sự cố đã vĩnh viễn không được đồng bộ sang bản snapshot tại Region B.
   - Đối với khách hàng, các câu truy vấn AI suy luận liên quan đến 4 tài liệu mới nhất này (ví dụ: hóa đơn vừa xuất, ticket hỗ trợ vừa tạo) sẽ không tìm thấy dữ liệu hoặc trả về thông tin lỗi thời. Do đó, tổ chức cần thiết lập cơ chế Write-Ahead Log (WAL) hoặc message queue (như Kafka) đa vùng để replay lại 4 dữ liệu này nhằm khôi phục tính toàn vẹn 100%.
