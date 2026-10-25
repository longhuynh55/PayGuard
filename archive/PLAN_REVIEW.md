# PayGuard — Kiểm chứng nhận định và phương án sửa plan

> **Lưu trữ (25/10/2026):** nội dung đã chuyển sang `docs/` theo module; file này không còn được cập nhật. Liên kết bên trong trỏ tới vị trí cũ.

Ngày rà soát: 25/10/2026. Đây là **lịch sử kiểm chứng và đề xuất của vòng review**. Sau research có mục tiêu, các quyết định được chọn đã tích hợp vào MASTER_PLAN.md / PROJECT_PLAN.md; protocol hiện hành nằm trong [RESEARCH_DECISIONS.md](RESEARCH_DECISIONS.md). Những lựa chọn khác nhau giữa đề xuất dưới đây và protocol lấy protocol làm chính.

## 1. Phạm vi và mức độ xác minh

Đã đọc hai plan, đối chiếu các nguồn gốc liên quan và chuẩn bị các ví dụ toán học/SQL chạy bằng Python chuẩn. Workspace tại thời điểm rà soát ban đầu chỉ có hai plan; chưa có simulator, model, dữ liệu, SQL feature hay log chạy để xác minh implementation.

Phân biệt ba loại kết luận:

- **Lỗi/thiếu sót trong đặc tả:** có thể xác nhận từ nội dung plan.
- **Rủi ro có điều kiện:** suy luận đúng, nhưng cần code/dữ liệu để biết có xảy ra hay không.
- **Giới hạn của kết luận:** thí nghiệm vẫn hợp lệ, nhưng không chứng minh được tuyên bố rộng hơn.

Các con số PR-AUC, 878K giao dịch, 89% nhãn đã về và 10/10 integrity checks là **số được plan báo cáo**, chưa được chạy lại ở đây. Rà soát này cũng không kiểm chứng toàn bộ danh sách related work hoặc thông tin API DeepSeek.

## 2. Kết quả kiểm chứng chín nhận định

### R1 — Calibration khi 89% nhãn sẽ về đã về

**Kết luận: rủi ro có điều kiện; nhận định trước “calibration sai/mâu thuẫn rõ” quá mạnh.** Không có quy tắc phổ quát rằng chỉ được calibration khi quan sát được 100% nhãn. Sai lệch phụ thuộc cách xử lý pending, độ trễ theo lớp/kênh, việc không bao giờ được báo cáo và mục tiêu cần hiệu chỉnh. Nghiên cứu gốc phân biệt phản hồi điều tra viên với nhãn trễ và chỉ ra selection bias từ cơ chế điều tra. [Dal Pozzolo et al., phần I và III](https://boracchi.faculty.polimi.it/docs/2017_FraudsTNNLS.pdf).

Thiếu sót xác nhận được ở MASTER mục 4-D: chưa định nghĩa nhãn âm, pending và cách đưa từng loại vào calibration. “89% số nhãn sẽ về” cũng không phải “89% toàn bộ giao dịch đã có nhãn đúng”.

**Sửa đề xuất:** thêm trạng thái nhãn, thời điểm khả dụng, cửa sổ trưởng thành theo kênh và chính sách xử lý dữ liệu thiếu. Giữ tháng 11 là phương án cần kiểm chứng; chọn mốc cuối cùng sau audit, không tự kết luận phải bỏ tháng này.

### R2 — Mẫu nhỏ/không có ý nghĩa thống kê → tự đóng

**Kết luận: thiếu sót logic xác nhận được.** MASTER mục 4-C ánh xạ trực tiếp `insignificant`, gồm cả mẫu nhỏ, sang tự đóng. PROJECT mục 5.1 lại mong “chưa đủ dữ liệu”. Đây là hai hành vi khác nhau. ASA nhấn mạnh p-value không cho xác suất giả thuyết đúng và quyết định không nên chỉ dựa vào một ngưỡng p-value. [ASA, nguyên tắc 2, 3 và 6](https://www.amstat.org/asa/files/pdfs/p-valuestatement.pdf).

**Sửa đề xuất:** thay nhãn gộp bằng nguyên nhân `legit_business_change` / `no_material_change` khi có căn cứ phù hợp; chuyển `insufficient_evidence` thành trạng thái bằng chứng. Mẫu nhỏ có thể được giữ mở chờ thêm dữ liệu hoặc chuyển người; tự đóng phải dựa vào chính sách rủi ro đã đánh giá, không chỉ dựa vào cỡ mẫu.

### R3 — Conformal một phần tử → an toàn

**Kết luận: đúng khi chỉ ra giới hạn, nhưng không được phủ nhận bảo đảm của KnowNo.** Split conformal kiểm soát coverage tổng thể dưới các giả định thích hợp; nó không tự cho bảo đảm tương đương trong riêng nhóm tự đóng hoặc riêng nhóm nguy hiểm. [Angelopoulos & Bates, phần 3.1 và định lý 1](https://arxiv.org/html/2107.07511v6).

KnowNo thực sự có bảo đảm hoàn thành nhiệm vụ, với phân phối tình huống và cơ chế trợ giúp được giả định trong bài. Quy tắc gốc hỏi người khi tập **không phải singleton**, bao gồm cả tập rỗng. [KnowNo, phần 3.2 / Proposition 1](https://proceedings.mlr.press/v229/ren23a/ren23a.pdf).

Ví dụ tự xây: 90 ca có tập `{benign, dangerous}` chứa đáp án; 10 ca nguy hiểm có tập `{benign}` sai. Coverage = 90%, lỗi tự đóng trên mọi ca = 10%, nhưng 100% ca tự đóng và 100% ca nguy hiểm bị xử lý sai. Ví dụ không mâu thuẫn với coverage tổng thể.

**Sửa đề xuất:** quy định tập rỗng, tách nhãn chẩn đoán và hành động, định nghĩa đúng mẫu số của rủi ro. Tách fit xác suất khỏi calibration conformal khi áp dụng split conformal chuẩn. Bảo đảm theo lớp của Mondrian ở model giao dịch không tự chuyển thành bảo đảm cho agent xử lý incident.

### R4 — 60 episode không đủ chứng minh lỗi gần 0

**Kết luận: xác nhận về giới hạn thống kê; 60 episode vẫn dùng được cho pilot.** Khi policy đã cố định và có n phép thử Bernoulli độc lập cùng phân phối, quan sát 0 lỗi cho cận trên một phía 95%: `p_upper = 1 - 0.05**(1/n)`. Cách này là trường hợp đặc biệt của khoảng exact binomial. [NIST](https://itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbino.htm).

Kết quả tính lại: n=24 → 11,73%; n=36 → 7,98%; cần n=299 với 0 lỗi để cận trên ≤1%. n là số ca thuộc **đúng mẫu số của metric**, không phải tổng episode bất kể loại. Những con số này không chứng minh khả năng tổng quát sang thế giới thật.

**Sửa đề xuất:** gọi 60 episode là pilot; dùng exact bounds cho tỷ lệ lỗi hiếm khi các giả định phù hợp. Các metric liên quan nhiều alert trong cùng episode cần xử lý phụ thuộc, chẳng hạn bootstrap theo episode. Với mẫu toàn số 0, bootstrap thông thường suy biến thành 0 và không phải bằng chứng không có rủi ro. Không chọn ngưỡng trên chính tập dùng để tuyên bố cận rủi ro.

### R5 — Chấm riêng agent có thể che lỗi toàn hệ thống

**Kết luận: thiếu metric end-to-end; cách chấm riêng agent tự nó không sai.** MASTER 4-A đã nói báo cáo monitoring bỏ sót riêng, nên không được nói plan hoàn toàn bỏ qua các ca này. Vấn đề là chưa định nghĩa một outcome chung cho mục tiêu business.

Ví dụ tự xây: 100 sự cố nguy hiểm, monitoring phát hiện 20, triage đúng 18. Điểm triage = 90%, tỷ lệ phát hiện và chuyển đúng toàn hệ thống = 18%.

**Sửa đề xuất:** giữ cả hai tầng metric; mọi baseline dùng cùng đầu vào monitoring. Lỗi rule tự đóng nằm trong metric toàn hệ thống, dù agent chưa từng thấy ca đó. Việc gửi đúng cho người chỉ chứng minh routing đúng, chưa chứng minh người đã xử lý đúng hoặc ngăn được tổn thất.

### R6 — Feature 24h / agent đọc tương lai

**Kết luận: rủi ro có điều kiện; chưa có bằng chứng implementation bị leakage.** Feature lịch sử 24h đã hoàn tất trước lúc điều tra là hợp lệ. Feature dùng 24h sau một giao dịch chưa đủ tuổi sẽ nhìn vào tương lai. Khôi phục feature tại đúng thời điểm là nguyên tắc của point-in-time retrieval. [Feast](https://docs.feast.dev/getting-started/concepts/point-in-time-joins).

**Sửa đề xuất:** mỗi run có `as_of_time`; phân biệt `event_at` và `available_at`. Nhãn chỉ đọc được sau `reported_at`. Aggregate 24h phải ghi thời điểm kết thúc quan sát và độ đầy đủ. Truth và cấu hình injector nằm ngoài database agent được truy cập. Chỉ nhắc trong prompt hoặc đặt tiền tố `_truth` chưa tạo được ranh giới truy cập.

### R7 — Ghép bằng thời gian + segment

**Kết luận: heuristic có thể dùng, nhưng đặc tả đang khẳng định đáp án mạnh hơn bằng chứng.** Hai sự cố có thể chồng thời gian/segment, tác động tới những metric khác nhau. “Không ghép được” không tự suy ra “vô hại”. Đây là phản ví dụ logic từ thiết kế injector, không phải khẳng định đã xảy ra trên dữ liệu PayGuard.

**Sửa đề xuất:** truth lưu tác động trực tiếp và gián tiếp dự kiến, entity, khoảng thời gian gồm độ kéo dài của cửa sổ 7 ngày. Giữ `unmatched` / `ambiguous_match` cho đánh giá chẩn đoán. Báo cáo số ca này và giữ chúng trong thống kê workload. Chấm routing theo tập hành động chấp nhận được của sự cố; không ép mọi alert về nguyên nhân nghiêm trọng nhất chỉ vì cùng segment.

### R8 — Unknown đối với rule khác unknown đối với hệ thống

**Kết luận: giới hạn diễn giải; không phải lỗi khiến benchmark hiện tại vô dụng.** PROJECT định nghĩa unknown theo những gì rule chưa thấy. Nếu calibration chứa cùng cơ chế với test, kết quả vẫn trả lời được câu hỏi “agent bổ sung được gì ngoài rule”, nhưng chưa chứng minh tổng quát hóa sang cơ chế hoàn toàn chưa gặp.

**Sửa đề xuất:** đánh dấu riêng `rule_covered`, `mechanism_seen_in_development`, `mechanism_seen_in_calibration`. Giữ test seed mới cùng cơ chế làm thí nghiệm chính nếu đó là mục tiêu. Thêm test giữ riêng cơ chế/tổ hợp như stress test khi đủ dữ liệu; không tuyên bố bảo đảm conformal tự chuyển sang tập này. Dùng chung lịch sử nền không tự làm các episode phụ thuộc nếu phần tương lai độc lập có điều kiện theo nền; nó giới hạn độ đa dạng thế giới được kiểm tra.

### R9 — Consistency không phải xác suất đúng

**Kết luận: đúng về giới hạn; phương pháp vẫn có bằng chứng thực nghiệm hữu ích.** Bài Maleki nghiên cứu execution-based consistency như một confidence score và báo cáo kết quả trên benchmark; không chứng minh mọi lần đồng thuận đều đúng. [Maleki et al., phần 3](https://arxiv.org/html/2508.14056v1).

Một database duy nhất có thể không phân biệt hai SQL khác nghĩa; đánh giá bằng test suite nhiều database được đề xuất để cải thiện kiểm tra ngữ nghĩa. [Zhong et al., EMNLP 2020](https://aclanthology.org/2020.emnlp-main.29/).

**Sửa đề xuất:** giữ execution consistency; đánh giá bằng câu hỏi có đáp án đã review, nhiều biến thể dữ liệu, và metric nhận ra câu hỏi mơ hồ/không trả lời được. Chạy calibration/test với đúng K, N, temperature, cache và quy tắc dừng được triển khai. Adaptive N không tất yếu gây overconfidence; nó thay đổi policy cần đo. Các lượt sinh có thể độc lập về sampling nhưng vẫn cùng thiên lệch có hệ thống.

## 3. Điểm bổ sung được xác minh

**CBPE:** NannyML fit trên reference có `y_true`, sau đó estimate trên analysis chưa có nhãn. Vì vậy câu “monitoring không cần nhãn” ở MASTER 4-D cần giới hạn cho PSI/KS và giai đoạn analysis; chưa đủ để chọn reference fit CBPE. [NannyML tutorial](https://nannyml.readthedocs.io/en/main/tutorials/performance_estimation/binary_performance_estimation/standard_metric_estimation.html). CBPE cũng không giải quyết được concept drift nói chung. [NannyML assumptions](https://nannyml.readthedocs.io/en/main/how_it_works/performance_estimation.html#assumptions-and-limitations).

**Calibration xác suất:** cần tách dữ liệu fit classifier khỏi fit calibrator. [scikit-learn](https://scikit-learn.org/stable/modules/calibration.html). Trong thiết kế split conformal đề xuất, cần thêm tập giữ riêng cho ngưỡng conformal sau khi toàn bộ predictor được cố định.

**Mùa vụ:** lịch sự kiện là bằng chứng bối cảnh; nó không loại trừ một sự cố fraud đồng thời. Đây đã là một kịch bản được PROJECT yêu cầu thử, nên nhánh rule tự đóng phải được chấm trên kịch bản đó.

**Workload:** số ticket tạo tự động không bằng số phút lao động tiết kiệm. Chẩn đoán đúng `new_fraud` vẫn có thể chuyển người. Điểm +1/−5 là utility giả định; chỉ gọi là chi phí kinh doanh khi có đơn vị và giả định chi phí cụ thể. Chưa có human study thì báo cáo runtime hệ thống và workload proxy.

## 4. Phương án chỉnh hai plan

Các mục dưới là lựa chọn thiết kế đề xuất cho MVP, không phải yêu cầu nguyên văn từ nguồn nghiên cứu.

### Bước 1 — Khóa hợp đồng dữ liệu trước khi tiếp tục P3

Trong MASTER 4-D, bổ sung:

- Bảng snapshot nhãn gồm `txn_id`, `label_state` (`confirmed_fraud`, `confirmed_legit`, `matured_negative`, `pending`), `label_available_at`, `source`, `label_policy_version`. Nhãn âm suy ra sau thời gian chờ phải phân biệt với nhãn âm đã xác minh.
- Quy tắc trưởng thành dựa vào kênh/tuổi giao dịch và chính sách đã cố định. Không chọn từng giao dịch vào tập “mature” bằng việc nhìn trước đáp án tương lai của nó.
- Báo cáo độ trễ và tỷ lệ không được báo cáo theo kênh, nguồn và fraud type bằng truth **chỉ ở evaluator**. Các tỷ lệ quan sát được trong vận hành ghi riêng.
- Ba vai trò dữ liệu: `model_fit`, `probability_calibration`, `conformal_calibration`; thêm validation cho lựa chọn model nếu có tuning. Tất cả thỏa giới hạn as-of; tập test được giữ riêng.
- Reference drift có thể dùng dữ liệu chưa nhãn. Reference CBPE phải đáp ứng điều kiện fit có nhãn, ngoài tập train model, và model/policy version phù hợp. Nếu CBPE tự calibration lại score, ghi rõ score nào đang được đánh giá.
- Lưu `as_of_time` và phiên bản snapshot cho mỗi score, alert, truy vấn và kết luận.

Trong PROJECT M1–M3, đồng bộ schema, timeline và cách báo cáo. Chưa quyết định tháng thay thế cho tháng 11 khi chưa kiểm tra dữ liệu thực tế.

**Điều kiện hoàn tất:** có bảng audit nhãn; truy vấn tại t không đổi khi thêm dữ liệu chỉ khả dụng sau t; truth không truy cập được từ tool agent; mỗi tập dữ liệu có manifest vai trò và mốc thời gian.

### Bước 2 — Tách nguyên nhân, mức bằng chứng và hành động

Trong MASTER 4-C và PROJECT M4, thay ánh xạ trực tiếp nguyên nhân → hành động bằng ba trường:

```text
cause_hypotheses: [seasonal, legit_business_change, data_pipeline,
                   fraud_surge, new_fraud, other]
evidence_status: sufficient | insufficient | conflicting
action: close | recheck | data_ticket | fraud_ops | human_review
```

- `cause_hypotheses` ghi các giả thuyết; trường riêng `coexisting_causes` chỉ dùng khi cần diễn đạt nhiều nguyên nhân đồng thời. Tập giả thuyết không mặc nhiên là tập nguyên nhân thật.
- `close` chỉ là ứng viên khi bằng chứng hỗ trợ thay đổi lành tính và vượt qua policy tự đóng đã đánh giá. Dữ liệu ít hoặc không có alert CBPE không đủ tạo điều kiện này.
- `recheck` phải có thời hạn, số lần chờ tối đa và bước chuyển người nếu vẫn thiếu bằng chứng; không tính là đã giải quyết.
- Rule và agent chịu cùng chính sách đóng cuối cùng. Log nguồn quyết định `rule` / `agent`.
- Tập conformal rỗng hoặc nhiều phần tử → tiếp tục điều tra / chuyển người theo ngân sách. Singleton `new_fraud` → chuyển người, singleton `data_pipeline` → đội data.
- MVP có thể dùng target hai lớp `benign_change` / `requires_intervention` cho thí nghiệm UQ ở bước quyết định; chẩn đoán nguyên nhân chi tiết là output riêng. Target và policy phải được chốt trước calibration. Không áp dụng kết quả của một target cho target khác.
- Conformal ở giao dịch và ở incident có dataset/alpha/metric riêng. Với alpha bất đối xứng của model, báo cáo coverage theo lớp, false approval, false decline, review; tỷ lệ review giảm tự nó chưa chứng minh policy tốt hơn.

**Điều kiện hoàn tất:** bảng hành vi cho mẫu nhỏ, seasonal+fraud, tập rỗng, đa giả thuyết, singleton nguy hiểm, tool lỗi và hết ngân sách. Ca cần nhiều đội xử lý có thể chuyển người kèm các routing cần thiết trong MVP.

### Bước 3 — Chốt evaluator và các mẫu số trước khi tuning

Trong MASTER 4-A/B và PROJECT 5, định nghĩa riêng:

| Metric | Mẫu số / ý nghĩa |
|---|---|
| Detection recall | Số sự cố cần can thiệp có ít nhất một alert phù hợp / tất cả sự cố cần can thiệp |
| End-to-end timely routing recall | Số sự cố cần can thiệp được phát hiện và chuyển đúng trong hạn / tất cả sự cố cần can thiệp |
| Dangerous closure rate | Số incident cần can thiệp bị tự đóng / số incident cần can thiệp đã tới triage |
| Unsafe fraction among closures | Số incident tự đóng nhưng cần can thiệp / tất cả incident tự đóng |
| Correct benign automation | Số incident lành tính được đóng đúng / tất cả incident lành tính đủ điều kiện đánh giá |
| Human workload | Số case, số lượt recheck, ticket và phút xử lý theo giả định ghi rõ; báo cáo cả alert lặp |
| Time and LLM cost | Từ thời điểm dữ liệu khả dụng tới quyết định, cùng chi phí API của toàn pipeline |

Ghi rõ `requires_intervention` bao gồm fraud và lỗi pipeline có tác động theo kịch bản; tránh dùng từ “nguy hiểm” với các nghĩa khác nhau giữa bảng điểm.

Ground truth phải lưu `incident_id`, nguyên nhân, phạm vi ảnh hưởng, `required_actions` và thời hạn tính từ mốc can thiệp. Một sự cố có nhiều alert chỉ được đếm một lần trong recall; giữ toàn bộ chi phí điều tra/recheck. Một lần đóng sai vẫn được ghi nhận kể cả sau đó incident được mở lại.

Xây bảng attribution riêng: `matched`, `ambiguous_match`, `unmatched`. Không gán tự động hai trạng thái sau thành benign. Nếu cần phân xử thủ công, người phân xử không được xem câu trả lời agent; khóa policy ghép trước test. Không loại những ca chưa ghép được khỏi báo cáo tổng số và workload; công khai denominator còn đủ đáp án cho accuracy.

### Bước 4 — Chia development / calibration / test theo mục đích

- `development`: viết rule, prompt, tool, score tổng hợp, ngưỡng monitoring và quy tắc ghép đáp án.
- `calibration`: chỉ hiệu chỉnh phần policy đã cố định. Với agent chỉ nhận nhánh còn lại sau rule, calibration phải phản ánh nhánh đó; 24 episode toàn bộ không chắc tạo ra 24 case cho agent.
- `test`: chạy policy đã khóa, không tune theo kết quả. Số lượng cuối cùng phụ thuộc số case từng nhóm; 60 episode là pilot, không phải bảo đảm có đủ cho mọi phép đo.
- Giữ riêng stress test cơ chế mới nếu muốn kết luận về generalization. Không dùng stress test để quảng bá bảo đảm coverage chưa được chứng minh.
- Baseline tối thiểu: rule + chuyển người; cùng rule + agent không UQ; cùng rule + agent có UQ; luôn chuyển người. Agent hai nhánh có prompt/tool/ngân sách tương đương hoặc báo cáo cost–quality curve để thấy ảnh hưởng sampling.
- Nếu mở rộng dữ liệu để báo cáo cận rủi ro, định trước cỡ mẫu hoặc dùng phương pháp theo dõi tuần tự phù hợp. Không liên tục thêm seed tới khi CI vừa đạt rồi dừng và coi đó là thí nghiệm cỡ mẫu cố định.

### Bước 5 — Đánh giá lại UQ và cập nhật tiêu chí “xong”

Trong PROJECT M4b / 5.2:

- Baseline N cố định trước, adaptive N là ablation sau; policy sampling, cache và version snapshot nằm trong cấu hình thực nghiệm.
- Cache key gồm question/prompt, model/version, sampling config, schema, episode, `as_of_time`, data snapshot; replay cache không được tính như mẫu độc lập mới.
- Bộ SQL có biến thể DB làm lộ sai mẫu số, sai join, duplicate, NULL, ngày biên và pending label. Expected behavior `clarify`/`abstain` được chấm riêng với execution accuracy.
- Đo confidence ở kết luận cuối; không nhân các confidence SQL để coi là xác suất đúng toàn chuỗi khi chưa có mô hình xác suất phù hợp.

Trong MASTER P2/P5:

- P2 hoàn tất khi có kiểm tra as-of, label policy, dữ liệu calibration tách đúng vai trò và báo cáo coverage/chi phí theo giai đoạn. Undercoverage sau drift là kết quả cần báo cáo, không phải ép coverage đạt rồi mới cho “xong”.
- P5 hoàn tất khi có bảng end-to-end, các mẫu số rõ ràng, uncertainty intervals phù hợp và kết quả known/unknown theo định nghĩa đã chốt. Không yêu cầu agent buộc phải thắng baseline.
- Trạng thái P1/P2 cũ giữ là “đã báo cáo hoàn thành; cần chạy lại các kiểm tra bổ sung” cho tới khi có code, thay vì tự tuyên bố các kết quả cũ sai.

## 5. Các kiểm tra thực tế cần chạy khi có code

| Kiểm tra | Bằng chứng cần có |
|---|---|
| Label audit | Các hàng pending không bị âm hóa ngoài policy; phân bố maturity/ascertainment theo nhóm; so observed-label với oracle-label chỉ trong evaluator |
| As-of invariance | Thêm/sửa dữ liệu tương lai và backfill chưa khả dụng không đổi feature/SQL tại thời điểm quá khứ |
| Truth isolation | Tool không thể đọc truth qua SQL trực tiếp, join, metadata hay file access |
| Routing | Seasonal+fraud, mẫu nhỏ, tập rỗng và hết budget ra hành động đúng theo policy; rule chịu cùng kiểm tra |
| Attribution | Hai sự cố chồng nhau, hiệu ứng trễ của window và alert không khớp không bị gán nhãn lành tính tùy tiện |
| Denominator accounting | Tổng episode/sự cố/alert/incident, nhánh rule/agent, ca bỏ sót và unresolved đều đối soát được |
| Split discipline | Manifest tách vai trò; không tune trên test; calibration agent phản ánh routing thực tế |
| SQL/UQ | Counterexample DB phân biệt query sai; đo fixed N và adaptive N với cùng budget được công bố |

## 6. Chạy lại các ví dụ kiểm chứng trong workspace này

```bash
python3 review/verify_plan_claims.py
```

Script ghi [verification_results.json](review/verification_results.json), gồm các ví dụ review ban đầu và kiểm tra bổ sung cho snapshot nhãn sửa đổi, tuổi cohort, gate và đơn vị thống kê theo protocol hiện hành.

Các ví dụ chỉ xác minh phép tính/phản ví dụ. Chúng không xác nhận simulator hoặc agent PayGuard mắc các lỗi tương ứng.
