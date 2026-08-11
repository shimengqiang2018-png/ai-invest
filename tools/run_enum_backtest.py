#!/usr/bin/env python3
"""枚举回测: 15只精选ETF的C(15,4)=455组合"""
import itertools, json, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from momentum_etf_backtest import run_backtest, ETF_POOL

CODES = ['159632','159659','159831','159834','159915','159934','159977',
         '510300','512480','512760','513100','513110','513310','513390','518880']

combos = list(itertools.combinations(CODES, 4))
total = len(combos)
print(f'C(15,4)={total} 组合 | 预计 ~{total*2//60}min', flush=True)

results = []
start = time.time()
for i, combo in enumerate(combos):
    pool = {c: ETF_POOL.get(c, c) for c in combo}
    try:
        r = run_backtest(pool=pool, start_date='2019-01-01', freq='biweekly',
                        momentum_period=40, include_bench=True, quiet=True)
        if r:
            p = r['performance']
            sells = [t for t in r['trades'] if '卖出' in t['action']]
            wins = len([t for t in sells if t.get('pnl',0)>0])
            wr = wins/len(sells)*100 if sells else 0
            results.append({
                'combo': '+'.join(combo), 'n': 4,
                'ann': round(p['annual_return_pct'],2),
                'total': round(p['total_return_pct'],2),
                'dd': round(p.get('max_dd_pct',0),1),
                'sharpe': round(p.get('sharpe',0),2),
                'calmar': round(p.get('calmar',0),2),
                'wr': round(wr,1), 'trades': p['num_trades'],
            })
    except Exception as e:
        pass
    if (i+1) % 50 == 0:
        e = time.time()-start
        rate = (i+1)/e
        eta = (total-i-1)/rate/60
        print(f'[{i+1}/{total}] {e:.0f}s | {rate:.1f}/s | ETA {eta:.1f}min', flush=True)

e = time.time()-start
results.sort(key=lambda r: r['ann'], reverse=True)

print(f'\n{"="*100}')
print(f'  枚举回测 TOP 30 | C(15,4) | 40日动量 | 2019起 | 耗时 {e/60:.1f}min')
print(f'{"="*100}')
hdr = f'{"排名":<4} {"组合":<55} {"年化":>8} {"总收益":>8} {"MaxDD":>7} {"Sharpe":>6} {"Calmar":>6} {"胜率":>5} {"交易":>4}'
print(hdr)
print('-'*len(hdr))
for i, r in enumerate(results[:30], 1):
    print(f'{i:<4} {r["combo"]:<55} {r["ann"]:>+7.1f}% {r["total"]:>+7.1f}% {r["dd"]:>6.1f}% {r["sharpe"]:>6.2f} {r["calmar"]:>6.2f} {r["wr"]:>4.0f}% {r["trades"]:>4d}')

top10_ann = [r['ann'] for r in results[:10]]
top10_sh = [r['sharpe'] for r in results[:10]]
all_ann = [r['ann'] for r in results]
print(f'\n📊 Top10年化均值: {sum(top10_ann)/len(top10_ann):.1f}% | Sharpe均值: {sum(top10_sh)/len(top10_sh):.2f}')
print(f'📊 全样本年化均值: {sum(all_ann)/len(all_ann):.1f}%')

best = results[0]
print(f'\n🥇 最佳: {best["combo"]} | 年化{best["ann"]:+.1f}% | Sharpe{best["sharpe"]:.2f} | MaxDD{best["dd"]:.1f}% | 胜率{best["wr"]:.0f}%')

OUT = os.path.join(os.path.dirname(__file__), '..', 'data', 'enum_backtest_15c3.json')
with open(OUT, 'w') as f:
    json.dump({'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'total_combos': total,
               'valid_results': len(results), 'config': 'C(15,4) 40日动量 biweekly 2019起',
               'results': results}, f, ensure_ascii=False)
print(f'\n✅ 完整结果保存到 {OUT}')
