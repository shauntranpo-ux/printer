from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def _overall_verdict(results: Dict[str, Any]) -> str:
    verdicts = [v.get('verdict', 'FAIL') for v in results.values()]
    if any(v == 'FAIL' for v in verdicts):
        return 'FAIL' if verdicts.count('FAIL') >= 2 else 'CONDITIONAL'
    if any(v == 'CONDITIONAL' for v in verdicts):
        return 'CONDITIONAL'
    return 'PASS'


def _verdict_emoji(v: str) -> str:
    return {'PASS': '✓', 'CONDITIONAL': '~', 'FAIL': '✗'}.get(v, '?')


def write_research_report(
    asset: str,
    results: Dict[str, Dict[str, Any]],
    output_dir: Path,
) -> None:
    """
    Write research_report.md and research.json to output_dir.
    results: {layer_key: layer_result_dict}
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    overall = _overall_verdict(results)

    # ── JSON ─────────────────────────────────────────────────────────────────
    json_out = {'asset': asset, 'overall_verdict': overall, 'layers': results}
    (output_dir / 'research.json').write_text(
        json.dumps(json_out, indent=2, default=str), encoding='utf-8'
    )

    # ── Markdown ─────────────────────────────────────────────────────────────
    lines = [
        f'# {asset} — Backtest Validation Report',
        '',
        f'**Overall verdict: {overall}**',
        '',
        '## Layer Summary',
        '',
        '| Layer | Verdict |',
        '|-------|---------|',
    ]
    layer_names = {
        'layer1': 'Layer 1 — Signal IC',
        'layer2': 'Layer 2 — Null Hypothesis',
        'layer3': 'Layer 3 — WFA Significance',
        'layer4': 'Layer 4 — Permutation Test',
        'layer5': 'Layer 5 — Regime Robustness',
    }
    for key, name in layer_names.items():
        if key in results:
            v = results[key].get('verdict', 'N/A')
            lines.append(f'| {name} | {_verdict_emoji(v)} {v} |')

    lines += ['', '---', '']

    # Layer 1 details
    if 'layer1' in results:
        r = results['layer1']
        lines += [
            '## Layer 1 — Signal IC',
            f"Failing signals: {r.get('n_failing', '?')}/{r.get('n_signals', '?')}",
            '',
            '| Signal | IC | ICIR | t-stat | Verdict |',
            '|--------|----|------|--------|---------|',
        ]
        for name, sig in r.get('signals', {}).items():
            lines.append(
                f"| {name} | {sig['ic']:.3f} | {sig['icir']:.3f} "
                f"| {sig['t_stat']:.2f} | {sig['verdict']} |"
            )
        lines.append('')

    # Layer 2 details
    if 'layer2' in results:
        r = results['layer2']
        lines += [
            '## Layer 2 — Null Hypothesis',
            f"Real Sharpe: {r.get('real_sharpe', '?'):.3f}  "
            f"Null 95th%: {r.get('null_p95', '?'):.3f}  "
            f"p-value: {r.get('p_value', '?'):.4f}",
        ]
        if r.get('lookahead_issues'):
            lines += ['', '**Lookahead findings:**']
            for issue in r['lookahead_issues']:
                lines.append(f'- {issue}')
        lines.append('')

    # Layer 3 details
    if 'layer3' in results:
        r = results['layer3']
        lines += [
            '## Layer 3 — WFA Significance',
            f"DSR: {r.get('dsr', '?'):.3f}  PBO: {r.get('pbo', '?'):.3f}  "
            f"MinBTL: {r.get('minbtl', '?'):.1f}yr (have {r.get('data_years', '?'):.1f}yr)",
            '',
        ]

    # Layer 4 details
    if 'layer4' in results:
        r = results['layer4']
        lines += [
            '## Layer 4 — Permutation Test',
            f"Trades: {r.get('n_trades', '?')}  Win rate: {r.get('win_rate', 0)*100:.1f}%  "
            f"p-value (block): {r.get('p_value_block', '?'):.4f}",
        ]
        if not r.get('sufficient_data', True):
            lines.append(f"⚠ Insufficient data — need {r.get('min_trades', '?')} trades minimum.")
        lines.append('')

    # Layer 5 details
    if 'layer5' in results:
        r = results['layer5']
        lines += ['## Layer 5 — Regime Robustness', '']
        rs = r.get('regime_sharpes', {})
        if rs:
            lines += ['| Regime | Sharpe |', '|--------|--------|']
            for regime, sr in sorted(rs.items()):
                lines.append(f'| {regime} | {sr:.3f} |')
        ss = r.get('session_sharpes', {})
        if ss:
            lines += ['', '| Session | Sharpe |', '|---------|--------|']
            for sess, sr in ss.items():
                lines.append(f'| {sess} | {sr:.3f} |')
        lines.append('')

    (output_dir / 'research_report.md').write_text('\n'.join(lines), encoding='utf-8')
