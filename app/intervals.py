"""半开区间 [start, end) 的集合运算。"""
from __future__ import annotations

from typing import List, Tuple

Interval = Tuple[str, str]


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """两个半开区间是否相交；端点相接（a_end == b_start）不算相交。"""
    return a_start < b_end and b_start < a_end


def subtract_intervals(start: str, end: str, cuts: List[Interval]) -> List[Interval]:
    """从 [start, end) 中挖掉若干区间，返回剩余片段（有序、互不相交）。

    cuts 中允许部分越界，函数按交集裁剪，因此调用方只需保证语义正确。
    比较基于定宽 UTC 字符串，字典序即时间序。
    """
    pieces: List[Interval] = [(start, end)]
    for cut_start, cut_end in sorted(cuts):
        remaining: List[Interval] = []
        for piece_start, piece_end in pieces:
            if cut_end <= piece_start or cut_start >= piece_end:
                # 完全不相交
                remaining.append((piece_start, piece_end))
                continue
            overlap_start = max(piece_start, cut_start)
            overlap_end = min(piece_end, cut_end)
            if piece_start < overlap_start:
                remaining.append((piece_start, overlap_start))
            if overlap_end < piece_end:
                remaining.append((overlap_end, piece_end))
        pieces = remaining
    return pieces


def contains(outer: Interval, inner: Interval) -> bool:
    """半开区间 outer 是否完整包含 inner（端点相等允许）。"""
    return outer[0] <= inner[0] and inner[1] <= outer[1]
