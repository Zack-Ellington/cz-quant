"""Preprocessing produces runtime inputs without accounts or trading."""
import json
from dataclasses import replace

import pytest
from czquant import Store

import prepare
from tests.test_trader import EVENT, FakeKalshi, FakePoly
from tests.test_trader_us import FakeUSVenue, SLUG
from tests.test_markets import LISTING, FakeKalshi as QuestionKalshi, FakePoly as QuestionPoly
from runner import load_experiment


def load_pair(tmp_path, pair):
    path = tmp_path / 'experiment.json'
    path.write_text(json.dumps({'id': 'prepared', 'portfolio': {'kalshi': 'k', pair['venue']: 'p'},
                               'mode': 'paper', 'pair': pair}))
    return load_experiment(str(path)).experiment.pair


@pytest.mark.parametrize('venue,client', [('polymarket-us', FakeUSVenue), ('polymarket', FakePoly)])
def test_public_preparation_exports_a_loadable_pair_without_a_store(tmp_path, monkeypatch, venue, client):
    monkeypatch.delenv('STRATEGY_RUNS_DIR', raising=False)
    monkeypatch.setattr(Store, '__init__', lambda *a, **k: pytest.fail('preprocessing must not open a ledger'))
    monkeypatch.setattr(prepare, 'Kalshi', lambda **kw: FakeKalshi())
    if venue == 'polymarket-us':
        monkeypatch.setattr(prepare.PolymarketUS, 'from_env', lambda **kw: client())
    else:
        monkeypatch.setattr(prepare.Polymarket, 'from_env', lambda: client())
    out = tmp_path / 'candidates.json'
    prepare.main(['--venue', venue, '--leagues', 'nba', '--out', str(out)])
    result = json.loads(out.read_text())
    assert result['counts'] == {'ok': 1}
    pair = load_pair(tmp_path, result['pairs'][0])
    assert pair.kalshi_event == EVENT and pair.kalshi_series == 'KXNBAGAME'
    assert pair.evidence['method'] == 'abbreviations'
    if venue == 'polymarket-us':
        assert pair.contracts('polymarket_us') == {('PMUS:' + SLUG, 'YES'), ('PMUS:' + SLUG, 'NO')}
    else:
        assert pair.contracts('polymarket') == {('PM:tok-bos', 'YES'), ('PM:tok-sac', 'YES')}


def test_pins_and_blocks_apply_only_during_preprocessing(tmp_path):
    kalshi, poly = FakeKalshi(), FakeUSVenue()
    event = next(poly.events())
    pins = {'blocked': [], 'pairs': {EVENT: {'slug': event['slug'],
              'legs': {EVENT + '-BOS': 'bos', EVENT + '-SAC': 'sac'}}}}
    result = prepare.sports(kalshi, poly, 'polymarket_us', prepare.load_leagues(('nba',)), pins, 6)
    assert load_pair(tmp_path, result['pairs'][0]).evidence['method'] == 'pinned'
    pins['blocked'].append(EVENT)
    blocked = prepare.sports(kalshi, poly, 'polymarket_us', prepare.load_leagues(('nba',)), pins, 6)
    assert blocked == {'pairs': [], 'counts': {'blocked': 1}}


@pytest.mark.parametrize('invert,yes_token', [(False, 'PM:tok-yes'), (True, 'PM:tok-no')])
def test_question_inversion_is_resolved_before_runtime(tmp_path, invert, yes_token):
    result = prepare.questions(QuestionKalshi(), QuestionPoly(), [replace(LISTING, invert=invert)])
    pair = load_pair(tmp_path, result['pairs'][0])
    assert pair.kalshi_event == 'KXFED-26DEC' and pair.kalshi_series == 'KXFED'
    assert next(o for o in pair.outcomes if o.kalshi_side == 'YES').polymarket == yes_token
    assert pair.evidence['invert'] == invert


@pytest.mark.parametrize('swapped', [False, True])
def test_reviewed_pin_resolves_aliases_but_cannot_change_an_exported_pair(tmp_path, monkeypatch, swapped):
    kalshi, poly = FakeKalshi(), FakeUSVenue()
    parse = prepare.kalshi_game

    def unmatched(event):
        game = parse(event)
        for team in game.teams:
            team['abbr'] = 'unmatched-' + team['abbr']
        return game

    monkeypatch.setattr(prepare, 'kalshi_game', unmatched)
    leagues = prepare.load_leagues(('nba',))
    assert prepare.sports(kalshi, poly, 'polymarket_us', leagues, {'blocked': [], 'pairs': {}}, 6)['pairs'] == []
    pins = {'blocked': [], 'pairs': {EVENT: {'slug': next(poly.events())['slug'],
            'legs': {EVENT + '-BOS': 'sac' if swapped else 'bos',
                     EVENT + '-SAC': 'bos' if swapped else 'sac'}}}}
    result = prepare.sports(kalshi, poly, 'polymarket_us', leagues, pins, 6)
    selected = load_pair(tmp_path, result['pairs'][0])
    assert selected.evidence['method'] == 'pinned'
    assert selected.outcomes[0].polymarket_side == ('NO' if swapped else 'YES')
    pins['pairs'][EVENT]['legs'] = {EVENT + '-BOS': 'bos' if swapped else 'sac',
                                   EVENT + '-SAC': 'sac' if swapped else 'bos'}
    assert load_experiment(str(tmp_path / 'experiment.json')).experiment.pair == selected
