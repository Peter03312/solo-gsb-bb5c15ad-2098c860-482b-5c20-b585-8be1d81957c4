#!/usr/bin/env bash
# 安全作业规程历史回溯服务 —— curl 调用示例
# 用法：bash examples/curl_examples.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
j() {  # 美化 JSON；非 JSON 输入原样输出，且不会让 pipefail 中断脚本
  local input; input=$(cat)
  printf '%s' "$input" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$input"
}

echo "### 0. 健康检查"
curl -s "$BASE/healthz"; echo

echo "### 1. 首次发布：编号 SOP-001，正文 + UTC 适用区间 [起,止)"
curl -s -X POST "$BASE/api/v1/procedures" \
  -H 'Content-Type: application/json' \
  -d '{
        "procedure_no": "SOP-001",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2026-12-31T23:59:59Z",
        "body": "进入受限空间作业规程（首版正文）"
      }' | j; echo

echo "### 2. 更正：被替代版本=1，仅覆盖其子区间 [03-01,04-01)，新正文"
curl -s -X POST "$BASE/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "procedure_no": "SOP-001",
        "supersedes_version": 1,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "进入受限空间作业规程（三月专项修订正文）"
      }' | j; echo

echo "### 3. 查询：作业时刻 2026-03-15，知悉时刻=现在（默认）→ 命中更正版"
curl -s "$BASE/api/v1/procedures/SOP-001/effective?work_at=2026-03-15T00:00:00Z" | j; echo

echo "### 4. 查询：作业时刻在更正区间之外（04-01 端点相接，半开区间）→ 仍是首版"
curl -s "$BASE/api/v1/procedures/SOP-001/effective?work_at=2026-04-01T00:00:00Z" | j; echo

echo "### 5. 历史回溯：知悉时刻取首版发布当时（更正发布之前）→ 3 月作业看到的仍是首版"
V1_PUBLISHED=$(curl -s "$BASE/api/v1/procedures/SOP-001/versions" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["versions"][0]["published_at"])')
curl -s -G "$BASE/api/v1/procedures/SOP-001/effective" \
  --data-urlencode "work_at=2026-03-15T00:00:00Z" \
  --data-urlencode "known_at=$V1_PUBLISHED" | j; echo

echo "### 6. 无匹配：作业时刻 2030 年，超出适用区间 → effective=null"
curl -s "$BASE/api/v1/procedures/SOP-001/effective?work_at=2030-01-01T00:00:00Z" | j; echo

echo "### 7. 并发更正（数组）：同批两条子区间重叠 → 整体 409，历史不变"
body=$(mktemp); code=$(curl -s -o "$body" -w '%{http_code}' -X POST "$BASE/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '[
        {"procedure_no":"SOP-001","supersedes_version":1,
         "valid_start":"2026-06-01T00:00:00Z","valid_end":"2026-07-01T00:00:00Z","body":"六月A"},
        {"procedure_no":"SOP-001","supersedes_version":1,
         "valid_start":"2026-06-15T00:00:00Z","valid_end":"2026-07-15T00:00:00Z","body":"六月B-重叠"}
      ]')
cat "$body" | j; echo "HTTP $code"; rm -f "$body"; echo

echo "### 8. 跨规程回溯：一次拿到截至知悉时刻、在作业时刻有效的所有规程正文"
curl -s "$BASE/api/v1/effective?work_at=2026-03-15T00:00:00Z" | j
