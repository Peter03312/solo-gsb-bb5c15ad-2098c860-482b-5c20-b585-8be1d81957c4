#!/usr/bin/env python3
"""安全作业规程历史回溯服务 —— Python 调用示例（仅用标准库 urllib）。

运行前：pip install requests 不是必需的；本示例零三方依赖。
用法：  python3 examples/python_client.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"


def call(method: str, path: str, body: dict | list | None = None,
         query: dict | None = None) -> tuple[int, dict]:
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n### {title}  [HTTP {status}]")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    # 1) 首次发布
    status, res = call("POST", "/api/v1/procedures", {
        "procedure_no": "SOP-001",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2026-12-31T23:59:59Z",
        "body": "进入受限空间作业规程（首版正文）",
    })
    show("首次发布（重复编号会返回 409 DUPLICATE_PROCEDURE）", status, res)

    # 2) 更正：只覆盖 [03-01, 04-01)
    status, res = call("POST", "/api/v1/corrections", {
        "procedure_no": "SOP-001",
        "supersedes_version": 1,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "进入受限空间作业规程（三月专项修订正文）",
    })
    show("提交更正", status, res)

    # 3) 双时间轴查询
    # 取首版的发布时刻作为知悉时刻：此时首版已发布、更正尚未发布
    _, versions = call("GET", "/api/v1/procedures/SOP-001/versions")
    v1_published = versions["data"]["versions"][0]["published_at"]
    status, res = call("GET", "/api/v1/procedures/SOP-001/effective", query={
        "work_at": "2026-03-15T00:00:00Z",   # 作业时刻
        "known_at": v1_published,             # 知悉时刻=首版发布当时（更正之前）
    })
    show("回溯：更正发布之前的知悉时刻 → 仍为首版", status, res)

    status, res = call("GET", "/api/v1/procedures/SOP-001/effective", query={
        "work_at": "2026-03-15T00:00:00Z",      # known_at 省略=服务端当前时刻
    })
    show("现在知悉：三月作业 → 更正版", status, res)

    # 4) 并发更正冲突：整批拒绝
    status, res = call("POST", "/api/v1/corrections", [
        {"procedure_no": "SOP-001", "supersedes_version": 1,
         "valid_start": "2026-06-01T00:00:00Z", "valid_end": "2026-07-01T00:00:00Z",
         "body": "六月A"},
        {"procedure_no": "SOP-001", "supersedes_version": 1,
         "valid_start": "2026-06-15T00:00:00Z", "valid_end": "2026-07-15T00:00:00Z",
         "body": "六月B（与A重叠）"},
    ])
    show("相互冲突的并发更正 → 整体拒绝，历史不变", status, res)

    # 5) 跨规程、无匹配为空
    status, res = call("GET", "/api/v1/effective",
                       query={"work_at": "2030-01-01T00:00:00Z"})
    show("跨规程回溯，无任何匹配 → results 为空列表", status, res)


if __name__ == "__main__":
    main()
