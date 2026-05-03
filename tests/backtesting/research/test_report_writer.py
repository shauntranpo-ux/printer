import json
from pathlib import Path
import pytest
from backtesting.research.report_writer import write_research_report

FAKE_RESULTS = {
    'layer1': {'verdict': 'PASS', 'n_failing': 2, 'n_signals': 8,
               'signals': {'supertrend_direction': {'ic': 0.05, 'icir': 0.4, 't_stat': 2.5,
                                                     'ic_decay': [0.05, 0.04, 0.03, 0.02],
                                                     'n_obs': 1000, 'verdict': 'PASS'}}},
    'layer2': {'verdict': 'PASS', 'p_value': 0.02, 'real_sharpe': 1.43,
               'null_p95': 0.61, 'n_trades': 312, 'lookahead_issues': []},
    'layer3': {'verdict': 'PASS', 'dsr': 0.97, 'pbo': 0.18, 'minbtl': 1.2,
               'data_years': 3.1, 'sr_obs': 1.43, 'num_trials': 50},
    'layer4': {'verdict': 'PASS', 'p_value_block': 0.004, 'real_sharpe': 1.43,
               'n_trades': 312, 'sufficient_data': True, 'win_rate': 0.59},
    'layer5': {'verdict': 'CONDITIONAL', 'regime_sharpes': {'low_trending': 1.82},
               'session_sharpes': {'US': 1.68}},
}


def test_writes_markdown_and_json(tmp_path):
    write_research_report('BTC', FAKE_RESULTS, output_dir=tmp_path)
    assert (tmp_path / 'research_report.md').exists()
    assert (tmp_path / 'research.json').exists()


def test_json_is_valid(tmp_path):
    write_research_report('BTC', FAKE_RESULTS, output_dir=tmp_path)
    data = json.loads((tmp_path / 'research.json').read_text())
    assert data['asset'] == 'BTC'
    assert 'overall_verdict' in data
    assert 'layers' in data


def test_markdown_contains_verdict(tmp_path):
    write_research_report('BTC', FAKE_RESULTS, output_dir=tmp_path)
    md = (tmp_path / 'research_report.md').read_text()
    assert 'BTC' in md
    assert 'PASS' in md or 'CONDITIONAL' in md or 'FAIL' in md


def test_overall_verdict_conditional_when_one_layer_fails(tmp_path):
    results = {**FAKE_RESULTS, 'layer5': {**FAKE_RESULTS['layer5'], 'verdict': 'FAIL'}}
    write_research_report('BTC', results, output_dir=tmp_path)
    data = json.loads((tmp_path / 'research.json').read_text())
    assert data['overall_verdict'] in ('CONDITIONAL', 'FAIL')
