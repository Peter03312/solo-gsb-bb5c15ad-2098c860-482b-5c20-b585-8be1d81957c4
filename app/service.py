"""核心领域逻辑：首次发布、更正（原子整体校验）、双时间轴回溯查询。

时间轴语义：
- valid（适用时间，作业时刻）：正文声明在现实中生效的半开区间 [start, end)。
- tx（事务时间，知悉时间 / published_at）：记录进入本服务、再不可改的时刻。

一条更正只在其基版“当前仍生效的子区间”中挖洞并放入新正文，形成一棵
更正树：基版未被覆盖的部分继续有效，更正正文在子区间内有效。查询时在
“截至知悉时刻已发布”的版本集合里，选作业时刻所处的最深版本。
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from .db import immediate_transaction
from .errors import ApiError
from .intervals import Interval, contains, overlaps, subtract_intervals
from .models import Version
from .timeutil import canonical, now_canonical, parse_utc


# ---------------------------------------------------------------- 输入校验

def _require(obj: object, key: str, field_cn: str, typ: type | tuple):
    if not isinstance(obj, dict) or key not in obj or obj[key] is None:
        raise ApiError(400, "INVALID_REQUEST", f"缺少必填字段: {field_cn}")
    value = obj[key]
    if not isinstance(value, typ):
        want = typ.__name__ if isinstance(typ, type) else " / ".join(t.__name__ for t in typ)
        raise ApiError(400, "INVALID_REQUEST", f"字段 {field_cn} 类型必须是 {want}")
    return value


def _parse_procedure_no(obj: dict) -> str:
    no = _require(obj, "procedure_no", "procedure_no（规程编号）", str)
    no = no.strip()
    if not no:
        raise ApiError(400, "INVALID_REQUEST", "规程编号不能为空")
    return no


def _parse_valid_interval(obj: dict) -> Interval:
    start = canonical(parse_utc(_require(obj, "valid_start", "valid_start（适用起）", str), "valid_start"))
    end = canonical(parse_utc(_require(obj, "valid_end", "valid_end（适用止）", str), "valid_end"))
    if not (start < end):
        raise ApiError(400, "ILLEGAL_INTERVAL", "适用区间非法：要求半开区间 [起, 止) 且 起 < 止")
    return start, end


def _parse_body(obj: dict) -> str:
    body = _require(obj, "body", "body（正文）", str)
    if not body.strip():
        raise ApiError(400, "INVALID_REQUEST", "正文不能为空")
    return body


def _parse_version_no(value: object, field: str) -> int:
    # JSON 数字或数字字符串均可，但必须是正整数
    if isinstance(value, bool):
        raise ApiError(400, "INVALID_REQUEST", f"字段 {field} 必须是正整数")
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            value = None
    if not isinstance(value, int) or value < 1:
        raise ApiError(400, "INVALID_REQUEST", f"字段 {field} 必须是正整数")
    return value


# ---------------------------------------------------------------- 首次发布

def publish_initial(conn: sqlite3.Connection, payload: dict) -> Version:
    """录入一份新规程的首版。重复编号整体拒绝（409）。"""
    procedure_no = _parse_procedure_no(payload)
    valid_start, valid_end = _parse_valid_interval(payload)
    body = _parse_body(payload)

    with immediate_transaction(conn) as tx:
        exists = tx.execute(
            "SELECT 1 FROM procedures WHERE procedure_no = ?", (procedure_no,)
        ).fetchone()
        if exists is not None:
            raise ApiError(409, "DUPLICATE_PROCEDURE", f"规程编号已存在: {procedure_no}")
        published_at = now_canonical()
        tx.execute(
            "INSERT INTO procedures(procedure_no, created_publish) VALUES (?, ?)",
            (procedure_no, published_at),
        )
        tx.execute(
            """INSERT INTO versions(procedure_no, version_no, valid_start, valid_end,
                                    body, published_at, supersedes_version)
               VALUES (?, 1, ?, ?, ?, ?, NULL)""",
            (procedure_no, valid_start, valid_end, body, published_at),
        )
    return get_version(conn, procedure_no, 1)


# ---------------------------------------------------------------- 更正

def publish_corrections(conn: sqlite3.Connection, payload) -> Version | List[Version]:
    """提交更正；payload 为单个更正对象或更正对象数组。

    - 单条：任一项不合法则整体拒绝（事务回滚）。
    - 数组（并发更正）：全部成功或全部拒绝；组内彼此重叠即冲突。
    """
    single = isinstance(payload, dict)
    items_raw = [payload] if single else payload
    if not isinstance(items_raw, list) or not items_raw:
        raise ApiError(400, "INVALID_REQUEST", "更正请求必须是一个更正对象或非空数组")

    # 事务外先解析所有输入（纯计算，不写历史）
    parsed = []
    for idx, item in enumerate(items_raw):
        if not isinstance(item, dict):
            raise ApiError(400, "INVALID_REQUEST", f"更正项 #{idx} 必须是对象")
        parsed.append({
            "procedure_no": _parse_procedure_no(item),
            "base_version_no": _parse_version_no(
                _require(item, "supersedes_version", "supersedes_version（被替代版本）",
                         (int, str)), "supersedes_version"),
            "interval": _parse_valid_interval(item),
            "body": _parse_body(item),
        })

    created: List[Version] = []
    with immediate_transaction(conn) as tx:
        # 规程必须已存在
        for p in parsed:
            row = tx.execute(
                "SELECT 1 FROM procedures WHERE procedure_no = ?", (p["procedure_no"],)
            ).fetchone()
            if row is None:
                raise ApiError(404, "PROCEDURE_NOT_FOUND",
                               f"规程编号不存在: {p['procedure_no']}")

        # 逐条在“当前历史 + 本批已接受的更正”上校验
        for idx, p in enumerate(parsed):
            procedure_no = p["procedure_no"]
            interval = p["interval"]
            base_no = p["base_version_no"]

            base_row = tx.execute(
                "SELECT * FROM versions WHERE procedure_no = ? AND version_no = ?",
                (procedure_no, base_no),
            ).fetchone()
            if base_row is None:
                raise ApiError(422, "BASE_VERSION_NOT_FOUND",
                               f"规程 {procedure_no} 的版本 {base_no} 不存在，无法作为被替代版本")
            base = Version.from_row(base_row)
            base_span: Interval = (base.valid_start, base.valid_end)

            # 1) 非法区间：子区间必须落在基版声明区间之内
            if not contains(base_span, interval):
                raise ApiError(
                    422, "ILLEGAL_INTERVAL",
                    f"子区间 [{interval[0]}, {interval[1]}) 超出被替代版本 {base_no} "
                    f"的生效区间 [{base_span[0]}, {base_span[1]})")

            # 计算基版“当前仍生效”的片段：声明区间减去其所有后代更正
            descendants = _descendants(tx, procedure_no, base_no)
            cuts: List[Interval] = [(d.valid_start, d.valid_end) for d in descendants]
            remaining = subtract_intervals(base.valid_start, base.valid_end, cuts)

            # 2) 过期基版：基版已被更正完全耗尽，任何子区间都不再生效
            if not remaining:
                raise ApiError(
                    409, "BASE_EXPIRED",
                    f"被替代版本 {base_no} 的生效区间已被后续更正完全覆盖，属于过期基版")

            inside_remaining = any(
                contains(piece, interval) for piece in remaining
            )
            if not inside_remaining:
                # 3) 目标子区间未完整落在基版当前生效片段内：
                #    若与同基版的已发布更正相交，属并发更正冲突
                sibling_rows = tx.execute(
                    """SELECT * FROM versions
                       WHERE procedure_no = ? AND supersedes_version = ?""",
                    (procedure_no, base_no),
                ).fetchall()
                for row in sibling_rows:
                    if overlaps(interval[0], interval[1], row["valid_start"], row["valid_end"]):
                        raise ApiError(
                            409, "CONCURRENT_CORRECTION",
                            f"更正与已发布的并发更正（版本 {row['version_no']}，"
                            f"均基于版本 {base_no}）在子区间上相互重叠")
                raise ApiError(
                    409, "BASE_EXPIRED",
                    f"被替代版本 {base_no} 在目标子区间上已不再生效，属于过期基版")

            # 4) 本批并发更正之间：同规程且子区间相交即整体冲突
            for earlier in parsed[:idx]:
                if earlier["procedure_no"] == procedure_no and overlaps(
                        interval[0], interval[1],
                        earlier["interval"][0], earlier["interval"][1]):
                    raise ApiError(
                        409, "CONCURRENT_CORRECTION",
                        "同批更正存在相互重叠的子区间，冲突更正必须整体拒绝")

        # 全部通过后统一发布（同一 published_at），历史只追加
        published_at = now_canonical()
        for p in parsed:
            next_no = tx.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM versions WHERE procedure_no = ?",
                (p["procedure_no"],),
            ).fetchone()["n"]
            tx.execute(
                """INSERT INTO versions(procedure_no, version_no, valid_start, valid_end,
                                        body, published_at, supersedes_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (p["procedure_no"], next_no, p["interval"][0], p["interval"][1],
                 p["body"], published_at, p["base_version_no"]),
            )
            created.append(get_version(tx, p["procedure_no"], next_no))

    return created[0] if single else created


def _descendants(tx: sqlite3.Connection, procedure_no: str, base_no: int) -> List[Version]:
    """返回以 base_no 为根的整棵更正子树（含直接/间接子版本）。"""
    result: List[Version] = []
    frontier = [base_no]
    seen = {base_no}
    while frontier:
        placeholders = ",".join("?" for _ in frontier)
        rows = tx.execute(
            f"""SELECT * FROM versions
                WHERE procedure_no = ? AND supersedes_version IN ({placeholders})""",
            [procedure_no, *frontier],
        ).fetchall()
        frontier = []
        for row in rows:
            v = Version.from_row(row)
            if v.version_no not in seen:
                seen.add(v.version_no)
                result.append(v)
                frontier.append(v.version_no)
    return result


# ---------------------------------------------------------------- 查询

def get_version(conn: sqlite3.Connection, procedure_no: str, version_no: int) -> Optional[Version]:
    row = conn.execute(
        "SELECT * FROM versions WHERE procedure_no = ? AND version_no = ?",
        (procedure_no, version_no),
    ).fetchone()
    return Version.from_row(row) if row else None


def find_effective(conn: sqlite3.Connection, procedure_no: str,
                   work_at: str, known_at: str) -> Optional[Version]:
    """单规程回溯：截至 known_at 已发布、在 work_at 有效的最深版本。

    无匹配（未发布/未到发布时间/作业时刻不在任何版本区间）返回 None。
    """
    rows = conn.execute(
        """SELECT * FROM versions
           WHERE procedure_no = ?
             AND published_at <= ?
             AND valid_start <= ? AND ? < valid_end
           ORDER BY version_no""",
        (procedure_no, known_at, work_at, work_at),
    ).fetchall()
    candidates = [Version.from_row(r) for r in rows]
    return _deepest(candidates)


def list_effective_all(conn: sqlite3.Connection, work_at: str, known_at: str) -> List[Version]:
    """跨规程回溯：每个规程至多返回一个在 work_at 有效的版本；无匹配为空列表。"""
    rows = conn.execute(
        """SELECT * FROM versions
           WHERE published_at <= ?
             AND valid_start <= ? AND ? < valid_end
           ORDER BY procedure_no, version_no""",
        (known_at, work_at, work_at),
    ).fetchall()
    by_procedure: dict[str, List[Version]] = {}
    for row in rows:
        by_procedure.setdefault(row["procedure_no"], []).append(Version.from_row(row))
    result = [_deepest(group) for group in by_procedure.values()]
    return [v for v in result if v is not None]


def _deepest(candidates: List[Version]) -> Optional[Version]:
    """从同规程、同作业时刻命中的候选中取最深（最新）版本。

    更正区间严格包含于基版区间，所以同一时刻命中的版本彼此祖先嵌套；
    最深者即更正链末端。若出现平级兄弟（理论上被校验排除），
    以发布时间更晚、版本号更大者为准，保证结果唯一。
    """
    if not candidates:
        return None
    by_no = {v.version_no: v for v in candidates}

    def depth(v: Version) -> int:
        d = 0
        cur = v
        seen = set()
        while cur.supersedes_version is not None and cur.supersedes_version in by_no \
                and cur.version_no not in seen:
            seen.add(cur.version_no)
            cur = by_no[cur.supersedes_version]
            d += 1
        return d

    return max(candidates, key=lambda v: (depth(v), v.published_at, v.version_no))


def list_versions(conn: sqlite3.Connection, procedure_no: str) -> List[Version]:
    rows = conn.execute(
        "SELECT * FROM versions WHERE procedure_no = ? ORDER BY version_no",
        (procedure_no,),
    ).fetchall()
    return [Version.from_row(r) for r in rows]


def list_procedures(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        "SELECT procedure_no, created_publish FROM procedures ORDER BY procedure_no"
    ).fetchall()
    return [{"procedure_no": r["procedure_no"], "created_publish": r["created_publish"]} for r in rows]
