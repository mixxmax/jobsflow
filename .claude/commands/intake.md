# /intake - 用户指定岗位：预览后确认入表

当用户已经在网页上看过某个岗位、直接提供 URL，或希望把外部看到的岗位交给
JobsFlow 时，使用统一入口，不要把它伪装成一次 `/scan`，也不要直接写 CSV/Sheets。

```bash
# 单个岗位：页面信息至少要提供职位名和用人公司
python3 -m tools.workflow intake \
  "https://example.test/jobs/123" \
  --title "Compliance Analyst" \
  --employer "Example Bank" \
  --platform jobsdb \
  --lane C \
  --jd-file /path/to/full-jd.txt

# 多个岗位：用 JSON 提供每个 URL 的页面/用户信息
python3 -m tools.workflow intake \
  --metadata-file /path/to/manual-intake.json \
  --fresh-title fresh_24h_2026-09-13

# 用户查看 proposal 后，才确认写入
python3 -m tools.workflow intake --confirm <proposal-id>
```

`metadata-file` 可以是一个对象（含 `items`）或列表；每项至少包含 `url`，并可
包含 `title`/`employer`/`platform`/`jd_text`/`page`/`lane`。页面或用户没有提供
职位名、用人公司时，系统会阻断而不会从 URL 猜测。平台会从 URL 自动标准化；同一
请求内、当前本地 ledger 和当前 CSV/Google Sheets 投影中的规范化 URL 都会去重。

有完整 JD 时调用既有评分器；没有完整 JD 时只保留 `JD深度=missing`、
`评估状态=待审-JD不足`，评分字段留空。缺少完整 JD 的岗位必须在页面信息或命令中
提供 lane，系统不会用中性分数冒充评分。JobsDB 缺全文时可自动经 gateway 附接主
Chrome 抓取；其他门户用 `--fetch-jd`（单次最多 3 个 URL）。已入表 URL 不再另开
更新链，改为 `materials prepare --job-id X [--jd-file F] [--fetch]`。

预览是完全无写入、无永久编号的 proposal，只列本次 URL 中的新岗位；重复项单独
报告。确认时才在同一条统一同步链上分配三位序号、写入本地 ledger/CSV/Sheets，
并绑定当前 proposal。确认前后会再次检查远端 digest 和 URL 重复，避免并发或重复入表。
