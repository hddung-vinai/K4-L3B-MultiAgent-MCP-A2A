# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống là một state-machine Python async thuần (không dùng LLM, tuân thủ giới hạn < 10B parameters). Mọi quyết định nghiệp vụ là luật tất định trên evidence MCP, nên cùng input + cùng evidence luôn cho cùng output.

```text
                      case_received (CLI)
                             │
                   ┌─────────▼──────────┐
                   │ Coordinator/Router │  (workflow.py)
                   └─────────┬──────────┘
                             │ task_assigned
                   ┌─────────▼──────────┐
                   │   Entity Resolver  │  resolve order + chọn lifecycle row theo opened_at
                   └─────────┬──────────┘
                             │ handoff (ENTITY_RESOLVED / AMBIGUOUS / NOT_FOUND)
                   ┌─────────▼──────────┐
                   │  Order/Item Agent  │  items, sellers, giá trị đơn trong scope
                   └─────────┬──────────┘
              ┌──────────────┴──────────────┐   (chạy song song)
     ┌────────▼─────────┐          ┌────────▼─────────┐
     │  Shipment Agent  │          │  Payment Agent   │
     └────────┬─────────┘          └────────┬─────────┘
              └──────────────┬──────────────┘ handoff → policy-agent
                   ┌─────────▼──────────┐
                   │    Policy Agent    │  chọn lifecycle row + primary_issue + rule → policy_decided
                   └─────────┬──────────┘
                   ┌─────────▼──────────┐
                   │ Conflict Resolver  │  ghi data_conflicts + nguồn được chọn
                   └─────────┬──────────┘
                   ┌─────────▼──────────┐
                   │      Verifier      │  invariants, calibration → verification_completed
                   └─────────┬──────────┘
                             ▼
             outputs/<case_id>.json + case_finalized (CLI)
```

Tất cả MCP call đi qua một `EvidenceCollector` duy nhất cho mỗi case (`evidence.py`). Collector này kiểm tra quyền, cache, retry và phát `tool_result_consumed`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer (`entity-agent`) | `candidate_order_ids`, `claimed_order_id`, `customer_unique_id_hint`, `opened_at` | Khớp candidate với lịch sử khách, loại candidate sai, chọn lifecycle row của lần mua bị khiếu nại | `get_customer_history`, `get_order` | `entity_resolution`, `customer_context`, `OrderScope` → handoff `coordinator` |
| Coordinator (`coordinator`) | Case input | Điều phối thứ tự, phát `task_assigned`, bỏ qua specialist khi entity `not_found` | Không gọi tool | Envelope tới từng agent |
| Order/product (`order-agent`) | Order ID + scope | Lọc item theo scope, lấy seller, tính giá trị đơn (price + freight), product context theo `investigation_scope` | `get_order_items`, `get_product_context`, `get_sellers` | `affected_entities` → handoff `policy-agent` |
| Shipment (`shipment-agent`) | Scope row + item scope | Xác định giao đúng hạn/trễ và bên gây trễ | `get_shipment_summary` | `shipment_analysis` → handoff `policy-agent` |
| Payment/refund (`payment-agent`) | Order ID + scope + giá trị đơn | Tổng capture, mismatch, duplicate, split hợp lệ, refund lifecycle | `get_payment_timeline`, `get_order_payments`, `get_refund_timeline` | `payment_analysis` → handoff `policy-agent` |
| Policy (`policy-agent`) | Findings của specialist | Phân loại `primary_issue` theo thứ tự ưu tiên, áp rule của `get_policy`, gắn seller thật của case | `get_policy` | `root_cause_analysis`, `financial_resolution`, `resolution_actions`, event `policy_decided` → handoff `verifier` |
| Conflict resolver (`conflict-agent`) | `get_order` row, scope row, tín hiệu shipment | So sánh các nguồn, ghi field mâu thuẫn và nguồn được chọn | Không gọi tool | `data_conflicts` → handoff `coordinator` |
| Verifier (`verifier`) | Toàn bộ findings | Lắp output, kiểm tra invariant, chọn evidence liên quan, tính confidence | Không gọi tool | Output cuối, event `verification_completed` → handoff `coordinator` |

Least privilege được enforce bằng code (`TOOL_PERMISSIONS` trong `evidence.py`). Agent gọi tool ngoài quyền sẽ bị `PermissionError` ngay, trước khi request được gửi tới MCP.

## 3. Entity resolution và A2A protocol

**Entity resolution**
1. Gọi `get_customer_history(customer_unique_id_hint)` và lấy giao giữa `candidate_order_ids` với order của khách.
   - Đúng 1 candidate khớp: `resolved`. Confidence là 0.95 nếu trùng `claimed_order_id`, ngược lại là 0.85.
   - Nhiều candidate khớp: `ambiguous`. Ưu tiên claimed ID, nếu không có thì chọn lần mua gần nhất trước `opened_at`.
   - Không candidate nào khớp: chỉ probe `get_order` với candidate có định dạng hợp lệ (32 hex), claimed ID trước. Không tìm được thì `not_found`.
2. Mọi candidate khác order được chọn đều đưa vào `rejected_candidates`. Candidate sai định dạng (ví dụ `candidate-001`) bị loại mà không tốn call.
3. **Chọn lifecycle row.** Một order ID có thể có nhiều row. Entity agent trả về danh sách row ứng viên (mua ≤ `opened_at`, mới nhất trước; các row trùng timestamp gộp làm một).
   - **Gán evidence cho row:** event/item thuộc row có timestamp **khớp chính xác** (ví dụ `delivered_late.event_at == order_delivered_customer_date`); nếu không khớp, gán cho row có purchase gần nhất ≤ event.
   - Specialist gọi MCP **một lần** rồi phân tích **từng row** (không tốn thêm call). Item trùng lặp hoàn toàn được gộp.
   - Policy agent chọn row: mặc định là row mới nhất; nếu claim của khách được evidence của row mặc định ủng hộ thì lấy claim đó; nếu chỉ một row cũ hơn (vẫn ≤ `opened_at`) ủng hộ claim thì chuyển sang row đó (confidence −0.07); nếu không row nào ủng hộ claim thì lấy issue ưu tiên cao nhất của row mặc định (hoặc `unsupported_claim`).

**A2A envelope** (`a2a.py`): `Envelope{case_id, sender, recipient, task, payload, message_id}`.
- Coordinator gửi `task_assigned` (target = agent, decision_code = task, `attributes.message_id`).
- Agent gửi `handoff` (target = agent kế tiếp, decision_code = kết luận ngắn như `SHIPMENT_SELLER_DELAY`, `attributes.in_reply_to` để liên kết request/response).
- Correlation theo `case_id`. Mỗi case có một `CaseContext` riêng (blackboard), không có state nghiệp vụ dùng chung giữa các case.
- **Chống vòng lặp:** luồng là DAG cố định (không có cạnh quay lại) và có thêm hop budget = 32 message/case. Vượt budget thì raise lỗi và case rơi về safe default.
- Trace chỉ chứa quyết định (decision code) và evidence ref, không chứa suy luận.

## 4. Evidence và conflict lifecycle

1. **Validate:** `EvidenceGateway.call` validate mọi response theo `mcp-evidence-response-v1`. Agent còn kiểm tra kiểu của `data` (dict hoặc list) trước khi dùng.
2. **Lưu `evidence_ref`:** lấy nguyên văn từ response, không bao giờ tự sinh hay sửa. Collector ghi ref vào `consumed_refs` và phát `tool_result_consumed(actor, tool_name, evidence_refs=[ref], attributes.domain)`.
3. **Map evidence vào output:** Verifier chọn evidence theo nhóm liên quan tới `primary_issue` (`ISSUE_EVIDENCE` trong `agents/policy.py`). Ví dụ lỗi giao trễ dùng entity + shipment + item + policy, lỗi refund dùng entity + payment + refund + policy. Ref nào không có trong `consumed_refs` sẽ bị loại. `claim_assessments[].evidence_refs` là tập con của `evidence_refs`.
4. **Source precedence khi mâu thuẫn:**
   - Row được scope theo `opened_at` từ `get_customer_history` được ưu tiên hơn row của `get_order` khi hai nguồn khác nhau ở `order_status`, `order_purchase_timestamp` hoặc `order_delivered_customer_date`. Resolution code là `SCOPED_TO_COMPLAINT_TIMELINE`.
   - Shipment event `delivered_late` có `status=confirmed` được ưu tiên hơn suy luận từ `shipping_limit_date`. Resolution code là `CONFIRMED_LIFECYCLE_EVENT_PRECEDENCE`.
   - Mâu thuẫn không giải quyết được thì `selected_source = null`.
5. Evidence không bao giờ được tái sử dụng giữa các case vì collector và cache được tạo mới cho từng case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / lỗi kết nối | 1 (backoff 1s) | Đánh dấu thiếu evidence cho domain đó, không suy đoán dữ liệu. Confidence −0.1 | `verification_completed` với `attributes.checks` |
| MCP tool error (không có dữ liệu cho scope) | 0 (lỗi tất định) | Domain được coi là không có sự kiện, ví dụ không có refund event | Không phát `tool_result_consumed` |
| Entity not found / ambiguous | 0 | `not_found`: bỏ qua specialist, `insufficient_evidence`, `needs_investigation`, confidence 0.3. `ambiguous`: vẫn điều tra, confidence −0.25 | `handoff` `ENTITY_NOT_FOUND` / `ENTITY_AMBIGUOUS` |
| Source conflict | 0 | Áp source precedence ở mục 4 và ghi `data_conflicts`. Nếu có mâu thuẫn trách nhiệm giao hàng thì confidence −0.15 | `handoff` `CONFLICTS_RECORDED` |
| Invalid specialist result / exception | 0 | Output mặc định an toàn theo schema, kèm evidence đã consume. Batch vẫn tiếp tục | `verification_completed` `FAILED_SAFE_DEFAULT` |

**Query budget (5–6 call/case, trung bình 5.5).** Coordinator lập kế hoạch điều tra theo claim (`planner.py`), specialist chỉ gọi họ tool mà kế hoạch cho phép:

| Claim | Tool gọi thêm ngoài phần cố định | Tổng |
| --- | --- | ---: |
| Mọi case (cố định) | `get_customer_history`, `get_order`, `get_product_context` (theo `include_product_context`), `get_policy` | 4 |
| `late_delivery_*` | `get_shipment_summary` | 5 |
| `payment_mismatch`, `canceled_order_paid`, `unavailable_order_paid` | `get_payment_timeline` | 5 |
| `valid_split_payment`, `duplicate_charge` | `get_order_items` (giá để so với tổng capture), `get_payment_timeline` | 6 |
| `refund_pending`, `refund_failed` | `get_payment_timeline`, `get_refund_timeline` | 6 |
| `unsupported_claim` | `get_shipment_summary`, `get_payment_timeline` (chứng minh không có lỗi) | 6 |
| Topic lạ | toàn bộ tool | 9 |

Khi không gọi `get_order_items`, item/product/seller ID lấy từ `get_product_context`. Field của domain không được điều tra giữ giá trị an toàn `insufficient_evidence`/`null` thay vì đoán.
- Không gọi `get_order_payments` vì `get_payment_timeline` đã chứa toàn bộ payment rows.
- Không gọi `get_sellers` vì item/product context đã có `seller_id`.
- Không gọi `get_order` cho candidate sai định dạng.
- Cache theo `(tool, args)` trong phạm vi case, nên mỗi cặp chỉ gọi tối đa một lần.

## 6. Verification invariants

Trước khi finalize, Verifier kiểm tra:
- **Schema:** output được dựng từ `empty_output()`, một khung đúng `l3b-output-v2` với `additionalProperties: false`. CLI validate lại toàn bộ trước khi ghi file.
- **Entity scope:** `affected_entities.order_ids` và `entity_resolution.resolved_order_ids` chỉ chứa order đã resolve. `rejected_candidates` chứa mọi candidate còn lại.
- **Evidence ownership:** mọi `evidence_refs` đều nằm trong `consumed_refs` của chính case và đã có event `tool_result_consumed`. Ref không đạt điều kiện này bị loại (`DROPPED_UNTRACED_REFS`).
- **Timeline:** mọi event và item dùng cho kết luận đều nằm trong scope `[purchase, next_purchase)`.
- **Payment/refund totals:** `recommended_refund_brl ≤ refundable_total_brl` (nếu vi phạm thì clamp, `REFUND_CLAMPED`). `refundable = captured − refunded ≥ 0`.
- **Status/refund/action:** `case_status = no_action` thì refund = 0 và `refund_lines` rỗng (`NO_ACTION_REFUND_ZEROED`). `resolution_actions` là `recommended_action` duy nhất của policy.
- **Responsibility:** `late_delivery_seller` thì bên chịu trách nhiệm là seller thật của case (không dùng `party_id` mẫu trong policy), và seller đó phải nằm trong `late_seller_ids`. Lỗi logistics hoặc payment thì không gán seller.
- **Confidence bounds:** mặc định 0.92 khi issue khớp claim. 0.7 khi evidence mâu thuẫn với claim. Giảm thêm khi entity ambiguous, khi có conflict trách nhiệm hoặc lỗi transient. Luôn kẹp trong [0.05, 0.97] và không bao giờ là 1.0. `insufficient_evidence` là 0.3.

## 7. Reproducibility

- **Model/config:** không dùng LLM. Rule-based, deterministic, không có random seed. `event_id` ngẫu nhiên theo `secrets` chỉ phục vụ định danh trace.
- **Dependency:** theo `pyproject.toml` (`mcp>=2,<3`, `jsonschema>=4.25`, `httpx2`, `python-dotenv`), Python ≥ 3.11. Starter gateway đã được vá để tương thích `mcp` 2.x (`CallToolResult.is_error`).
- **Concurrency:** tối đa 4 case chạy đồng thời trên cùng một MCP session (`CASE_CONCURRENCY` trong `cli.py`). Trong một case, shipment agent và payment agent chạy song song, còn lại tuần tự. Mỗi case có collector/cache riêng nên không lẫn evidence.
- **Lệnh chạy:** `day09 validate-inputs` → `day09 run` → `day09 validate` → `day09 package --output dist/submission.zip`.
- **Kiểm thử offline:** đặt `DAY09_DUMP_DIR=debug/mcp` khi `day09 run` để lưu response thô (thư mục bị gitignore, không bao giờ vào ZIP). `python scripts/replay.py` chạy lại workflow trên dữ liệu đã lưu mà không gọi MCP.
- **Tài nguyên:** khoảng 5.5 MCP call/case, tức khoảng 550 call cho 100 case. Timeout HTTP 300s (connect 30s). Không ghi API key vào code, trace hay output.
