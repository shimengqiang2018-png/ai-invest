#!/usr/bin/env python3
"""全市场扫描结果 → 动量轮动预设池。

读取 etf_full_backtest.py 的全市场评分（data/etf_backtest_results.json），
按类别分层 + 波动率甜区 + 流动性过滤 + 组合内相关性去重，选出 9-12 只的
动量轮动候选池；可选合并 etf_screener.py 的四维选品分（--screener-json）。

本脚本只负责「选股」：候选池的最终存储与生效由 dashboard 写入 MySQL
momentum_pools 表（页面「生成预设池 → 保存为预设池」），下游枚举/回测/
信号扫描一律从 momentum_pools 读取，不再依赖本脚本内置代码列表。

用法:
    python3 tools/build_preset_pool.py --screener-json data/cache/screener_latest.json
    python3 tools/build_preset_pool.py --json
    python3 tools/build_preset_pool.py --target-size 10 --max-corr 0.85
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict


_PROJECT_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
# 优先使用全市场扫描（/api/etf-scan/recalc 或 etf_full_backtest.py）写入的最新结果；
# 尚未跑过扫描时回退到仓库内旧快照。
_CACHE_SOURCE = os.path.join(_PROJECT_DATA, "cache", "etf_backtest_results.json")
DEFAULT_SOURCE = (
    _CACHE_SOURCE
    if os.path.exists(_CACHE_SOURCE)
    else os.path.join(_PROJECT_DATA, "etf_backtest_results.json")
)

# 目标类别分布（最少 / 最多）：保证宽基、跨境、行业、另类都覆盖，避免池子同涨同跌。
CATEGORY_BOUNDS = {
    "A股宽基": (2, 4),
    "跨境ETF": (2, 3),
    "A股行业": (2, 3),
    "商品ETF": (1, 2),
}


def _return_series(code: str) -> dict[str, float] | None:
    """从本地缓存取日 K，返回 {date: 对数收益}；无可用数据返回 None。"""
    try:
        os.environ.setdefault("ETF_DATA_OFFLINE", "1")
        from etf_market_data import load_etf_series

        series = load_etf_series(code, count=2000)
        bars = series.bars if series else ()
        if not bars:
            return None
        out: dict[str, float] = {}
        prev = None
        for bar in bars:
            close = float(bar["close"])
            if prev and prev > 0 and close > 0:
                out[str(bar["date"])] = math.log(close / prev)
            prev = close
        return out or None
    except Exception:
        return None


def _pair_corr(ret_a: dict[str, float], ret_b: dict[str, float]) -> float | None:
    """两个标的在共同交易日上的 Pearson 相关；样本不足返回 None。"""
    common = sorted(set(ret_a) & set(ret_b))
    if len(common) < 30:
        return None
    xs = [ret_a[d] for d in common]
    ys = [ret_b[d] for d in common]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / (n - 1))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / (n - 1))
    if sx <= 0 or sy <= 0:
        return None
    return max(-1.0, min(1.0, cov / (sx * sy)))


def _avg_daily_amount(code: str, window: int = 60) -> float | None:
    """近 window 个交易日的日均成交额（亿元），无可用数据返回 None。

    缓存 K 线 volume 单位为手（1手=100份），成交额 = volume×100×close。
    """
    try:
        os.environ.setdefault("ETF_DATA_OFFLINE", "1")
        from etf_market_data import load_etf_series

        series = load_etf_series(code, count=2000)
        bars = list(series.bars)[-window:]
        if not bars:
            return None
        amounts = [float(b["volume"]) * 100 * float(b["close"]) / 1e8 for b in bars]
        return sum(amounts) / len(amounts)
    except Exception:
        return None


def _load_screener_scores(path: str) -> dict[str, dict]:
    """读取 etf_screener.py --json 的输出，返回 {code: 四维评分行}。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return {str(r.get("code")): r for r in (data.get("results") or [])}
    except (OSError, json.JSONDecodeError):
        return {}


def _compute_screener_scores(
    codes: list[str], names: dict[str, str]
) -> dict[str, dict]:
    """对候选逐只计算四维分（方向/流动性/独立性/波动率），返回 {code: {...}}。

    复用 etf_screener 的指标函数与腾讯 K 线 6 小时缓存；独立性按候选池
    内部平均相关计算，保证四维分与最终池子口径一致。单只失败不影响其余候选。
    """
    try:
        from etf_screener import (
            fetch_kline,
            calc_cagr,
            calc_annual_vol,
            calc_max_drawdown,
            calc_avg_daily_amount,
            calc_rolling_returns,
            calc_correlation,
            score_direction,
            score_liquidity,
            score_volatility,
        )
    except ImportError:
        from tools.etf_screener import (
            fetch_kline,
            calc_cagr,
            calc_annual_vol,
            calc_max_drawdown,
            calc_avg_daily_amount,
            calc_rolling_returns,
            calc_correlation,
            score_direction,
            score_liquidity,
            score_volatility,
        )

    out: dict[str, dict] = {}
    roll: dict[str, dict[str, float]] = {}
    meta: dict[str, dict] = {}
    for code in codes:
        try:
            klines = fetch_kline(code, count=800)
            if not klines or len(klines) < 250:
                continue
            meta[code] = {
                "name": names.get(code, ""),
                "cagr": calc_cagr(klines),
                "vol": calc_annual_vol(klines),
                "max_dd": calc_max_drawdown(klines),
                "avg_amt": calc_avg_daily_amount(klines),
                "days": len(klines),
            }
            roll[code] = calc_rolling_returns(klines, 60)
        except Exception:
            continue

    for code in meta:
        ac_vals = [
            calc_correlation(roll[code], roll[other])
            for other in meta
            if other != code
        ]
        ac = sum(ac_vals) / len(ac_vals) if ac_vals else 1.0
        d = meta[code]
        s_dir, _ = score_direction(d["cagr"])
        s_liq, _ = score_liquidity(d["avg_amt"])
        s_vol, _ = score_volatility(d["vol"])
        if ac < 0.3:
            s_corr = 5
        elif ac < 0.45:
            s_corr = 4
        elif ac < 0.6:
            s_corr = 3
        elif ac < 0.75:
            s_corr = 2
        else:
            s_corr = 1
        out[code] = {
            "total": s_dir + s_liq + s_corr + s_vol,
            "s_dir": s_dir,
            "s_liq": s_liq,
            "s_corr": s_corr,
            "s_vol": s_vol,
            "cagr": d["cagr"],
            "vol": d["vol"],
            "max_dd": d["max_dd"],
            "avg_amt": d["avg_amt"],
            "avg_corr": round(ac, 3),
            "days": d["days"],
        }
    return out


def select_preset_pool(
    results: list[dict],
    *,
    target_size: int = 10,
    min_score: float = 30.0,
    min_vol: float = 0.12,
    max_vol: float = 0.45,
    min_size: float = 1e8,
    min_turnover: float = 0.0,
    category_bounds: dict[str, tuple[int, int]] | None = None,
    max_corr: float = 0.85,
    corr_buffer: int = 8,
    screener_scores: dict[str, dict] | None = None,
) -> dict:
    """按类别分层 + 相关性去重选出候选池。

    返回 {"candidates": [...], "removed_no_data": [...], "removed_corr": [...]}。
    candidates 每项含 avg_corr（组合内平均相关）与 screener_total（四维总分，有则填）。
    """
    bounds = category_bounds or CATEGORY_BOUNDS
    screener = dict(screener_scores or {})
    eligible = [
        r
        for r in results
        if (r.get("composite_score") or 0) >= min_score
        and min_vol <= (r.get("volatility") or 0) <= max_vol
        and (r.get("fund_size") or 0) >= min_size
    ]
    if min_turnover > 0:
        eligible = [
            r for r in eligible
            if (_avg_daily_amount(str(r.get("code") or "")) or 0.0) >= min_turnover
        ]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        by_cat[str(r.get("category") or "其他")].append(r)
    for items in by_cat.values():
        items.sort(key=lambda r: r.get("composite_score", 0), reverse=True)

    shortlist: list[dict] = []
    picked: set[str] = set()

    def _pick(items: list[dict], count: int) -> list[dict]:
        out = []
        for r in items:
            if len(out) >= count:
                break
            code = str(r.get("code") or "")
            if code in picked:
                continue
            out.append(r)
            picked.add(code)
        return out

    # 第一轮：每个类别保证下限
    for cat, (lo, _hi) in bounds.items():
        shortlist.extend(_pick(by_cat.get(cat, []), lo))
    for cat in sorted(by_cat):
        if cat not in bounds:
            shortlist.extend(_pick(by_cat[cat], 1))
    # 第二轮：轮转补齐到 target_size + corr_buffer（给相关性去重留余量）
    target_shortlist = target_size + corr_buffer
    while len(shortlist) < target_shortlist:
        best = None
        for cat, (_lo, hi) in bounds.items():
            count = sum(1 for r in shortlist if r.get("category") == cat)
            if count >= hi + 2:  # 短名单阶段放宽上限，交给相关性去重
                continue
            candidate = next(
                (r for r in by_cat.get(cat, []) if str(r.get("code") or "") not in picked),
                None,
            )
            if candidate is not None and (
                best is None
                or candidate.get("composite_score", 0) > best.get("composite_score", 0)
            ):
                best = candidate
        if best is None:
            break
        shortlist.append(best)
        picked.add(str(best.get("code") or ""))

    # 预取收益率序列：无本地缓存的标的直接剔除
    returns: dict[str, dict[str, float]] = {}
    removed_no_data = []
    for r in shortlist:
        code = str(r.get("code") or "")
        ret = _return_series(code)
        if ret is None:
            removed_no_data.append({"code": code, "name": str(r.get("name") or "")})
        else:
            returns[code] = ret

    # 相关性去重（贪心）：按综合分从高到低，与已保留标的平均相关 > 阈值则剔除
    corr_cache: dict[tuple[str, str], float | None] = {}

    def _corr(a: str, b: str) -> float | None:
        key = tuple(sorted((a, b)))
        if key not in corr_cache:
            corr_cache[key] = _pair_corr(returns[a], returns[b])
        return corr_cache[key]

    ordered = sorted(
        (r for r in shortlist if str(r.get("code") or "") in returns),
        key=lambda r: r.get("composite_score", 0),
        reverse=True,
    )
    kept: list[dict] = []
    removed_corr: list[dict] = []
    for r in ordered:
        if len(kept) >= target_size:
            break
        code = str(r.get("code") or "")
        maxc = 0.0
        worst_with = ""
        for k in kept:
            c = _corr(code, str(k.get("code") or ""))
            if c is None:
                continue
            if c > maxc:
                maxc = c
                worst_with = str(k.get("code") or "")
        if not kept or maxc <= max_corr:
            kept.append(r)
        else:
            removed_corr.append({
                "code": code,
                "name": str(r.get("name") or ""),
                "max_corr": round(maxc, 3),
                "with_code": worst_with,
            })
    # 若去重后不足 target_size：先从完整合格名单里找未被评估、且与已保留标的
    # 低相关的候选补齐（保持主题多样性，避免把同主题高相关标的补回来）；
    # 只有合格名单用尽后才按综合分补回被剔除者并标注 corr 超限。
    if len(kept) < target_size:
        kept_codes = [str(r.get("code") or "") for r in kept]
        shortlist_codes = {str(r.get("code") or "") for r in shortlist}

        def _cat_hi(cat: str) -> int:
            """类别上限：bounds 内取 hi，bounds 外（LOF/REITs 等）最多 1 只。"""
            return bounds.get(cat, (0, 1))[1]

        all_ordered = sorted(
            eligible, key=lambda r: r.get("composite_score", 0), reverse=True
        )
        for r in all_ordered:
            if len(kept) >= target_size:
                break
            code = str(r.get("code") or "")
            if code in shortlist_codes or code in kept_codes:
                continue
            cat = str(r.get("category") or "其他")
            cat_count = sum(1 for k in kept if str(k.get("category") or "其他") == cat)
            if cat_count >= _cat_hi(cat):
                continue
            if code not in returns:
                ret = _return_series(code)
                if ret is None:
                    continue
                returns[code] = ret
            maxc = 0.0
            for k in kept:
                c = _corr(code, str(k.get("code") or ""))
                if c is not None and c > maxc:
                    maxc = c
            if kept and maxc > max_corr:
                continue
            kept.append(r)
            kept_codes.append(code)
    if len(kept) < target_size:
        for dropped in sorted(
            removed_corr, key=lambda d: -_score_of(ordered, d["code"])
        ):
            if len(kept) >= target_size:
                break
            r = next(x for x in ordered if str(x.get("code") or "") == dropped["code"])
            r["corr_override"] = f"与 {dropped['with_code']} 相关 {dropped['max_corr']}"
            kept.append(r)

    # 计算每个保留标的的组合内平均相关性
    kept_codes = [str(r.get("code") or "") for r in kept]
    avg_corr_by_code: dict[str, float] = {}
    for code in kept_codes:
        vals = [
            c for other in kept_codes
            if other != code and (c := _corr(code, other)) is not None
        ]
        avg_corr_by_code[code] = sum(vals) / len(vals) if vals else 0.0

    # 四维分：优先用调用方传入的 screener 结果；缺失的候选现场计算补上。
    missing = [c for c in kept_codes if c not in screener]
    if missing:
        names = {str(r.get("code") or ""): str(r.get("name") or "") for r in kept}
        computed = _compute_screener_scores(missing, names)
        screener.update(computed)

    candidates = []
    for r in sorted(kept, key=lambda x: x.get("composite_score", 0), reverse=True):
        code = str(r.get("code") or "")
        entry = {
            "code": code,
            "name": str(r.get("name") or ""),
            "category": str(r.get("category") or ""),
            "subcategory": str(r.get("subcategory") or ""),
            "composite_score": r.get("composite_score"),
            "volatility": r.get("volatility"),
            "annual_return": r.get("annual_return"),
            "sharpe_ratio": r.get("sharpe_ratio"),
            "max_drawdown": r.get("max_drawdown"),
            "fund_size": r.get("fund_size"),
            "avg_corr": round(avg_corr_by_code.get(code, 0.0), 3),
            "corr_override": r.get("corr_override"),
        }
        sc = screener.get(code)
        if sc:
            entry["screener_total"] = sc.get("total")
            entry["screener"] = {
                "s_dir": sc.get("s_dir"),
                "s_liq": sc.get("s_liq"),
                "s_corr": sc.get("s_corr"),
                "s_vol": sc.get("s_vol"),
            }
        candidates.append(entry)

    return {
        "candidates": candidates,
        "removed_no_data": removed_no_data,
        "removed_corr": removed_corr,
    }


def _score_of(ordered: list[dict], code: str) -> float:
    for r in ordered:
        if str(r.get("code") or "") == code:
            return float(r.get("composite_score") or 0)
    return 0.0


def _load_source(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取全市场扫描结果 {path}: {exc}")


def main():
    parser = argparse.ArgumentParser(description="全市场扫描 → 动量预设池")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="全市场评分 JSON 路径")
    parser.add_argument("--target-size", type=int, default=10, help="预设池标的数（默认10）")
    parser.add_argument("--min-score", type=float, default=30.0, help="综合分下限（默认30）")
    parser.add_argument("--min-vol", type=float, default=0.12, help="年化波动率下限（默认0.12）")
    parser.add_argument("--max-vol", type=float, default=0.45, help="年化波动率上限（默认0.45）")
    parser.add_argument("--min-size", type=float, default=1e8, help="基金规模下限（默认1亿）")
    parser.add_argument("--min-bars", type=int, default=0,
                        help="最少历史K线条数（排除次新ETF，默认0=不限制；如 1000 ≈ 4年）")
    parser.add_argument("--min-turnover", type=float, default=0.0,
                        help="日均成交额下限（亿元，默认0=不限制；如 1 = 近60日日均≥1亿）")
    parser.add_argument("--max-corr", type=float, default=0.85,
                        help="组合内相关性上限（默认0.85，超过则去重）")
    parser.add_argument("--screener-json", default=None,
                        help="etf_screener.py --json 输出路径（合并四维分，可选）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（含元信息）")
    args = parser.parse_args()

    source = _load_source(args.source)
    results = source.get("results") or []
    if args.min_bars > 0:
        before = len(results)
        results = [
            r for r in results
            if (r.get("bars_count") or r.get("n_bars") or 0) >= args.min_bars
        ]
        print(f"📏 排除次新ETF（bars>={args.min_bars}）: {before} → {len(results)} 只")
    screener_scores = _load_screener_scores(args.screener_json)
    selection = select_preset_pool(
        results,
        target_size=args.target_size,
        min_score=args.min_score,
        min_vol=args.min_vol,
        max_vol=args.max_vol,
        min_size=args.min_size,
        min_turnover=args.min_turnover,
        max_corr=args.max_corr,
        screener_scores=screener_scores,
    )
    candidates = selection["candidates"]
    if len(candidates) < 3:
        raise SystemExit(
            f"可用标的仅 {len(candidates)} 只（<3），请先联网重新扫描全市场"
        )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": os.path.basename(args.source),
        "source_generated_at": source.get("generated_at"),
        "source_total_tested": source.get("total_tested"),
        "target_size": args.target_size,
        "params": {
            "min_score": args.min_score,
            "min_vol": args.min_vol,
            "max_vol": args.max_vol,
            "min_size": args.min_size,
            "min_bars": args.min_bars,
            "min_turnover": args.min_turnover,
            "max_corr": args.max_corr,
        },
        "candidates": candidates,
        "removed_no_data": selection["removed_no_data"],
        "removed_corr": selection["removed_corr"],
    }

    if args.json:
        print("__JSON_START__")
        print(json.dumps(payload, ensure_ascii=False))
        print("__JSON_END__")
        return

    print("=" * 110)
    print(f"  全市场 → 动量预设池（候选 {len(candidates)} 只 / 目标 {args.target_size} 只）")
    print(f"  评分来源: {os.path.basename(args.source)}（生成于 {source.get('generated_at', '?')}）")
    print("=" * 110)
    header = (
        f"  {'#':<3s} {'代码':<8s} {'名称':<16s} {'类别':<10s} "
        f"{'综合分':>7s} {'平均相关':>8s} {'四维分':>6s} {'波动':>7s} {'年化':>7s} {'Sharpe':>7s}"
    )
    print(header)
    print("  " + "-" * 106)
    for i, c in enumerate(candidates, 1):
        corr_text = f"{c['avg_corr']:.2f}" + ("⚠" if c.get("corr_override") else "")
        screener_text = str(c.get("screener_total") or "—")
        print(
            f"  {i:<3d} {c['code']:<8s} {c['name'][:14]:<16s} {c['category']:<10s} "
            f"{c['composite_score']:>6.1f} {corr_text:>8s} {screener_text:>6s} "
            f"{c['volatility'] * 100:>6.1f}% {c['annual_return'] * 100:>+6.1f}% "
            f"{c['sharpe_ratio']:>7.2f}"
        )
    if selection["removed_no_data"]:
        print("\n  ⚠️ 无本地行情缓存被剔除:")
        for d in selection["removed_no_data"]:
            print(f"    {d['code']} {d['name']}（请先联网重新扫描全市场）")
    if selection["removed_corr"]:
        print("\n  ⚠️ 组合内相关性超限被剔除:")
        for d in selection["removed_corr"]:
            print(f"    {d['code']} {d['name']}（与 {d['with_code']} 相关 {d['max_corr']}）")


if __name__ == "__main__":
    main()
