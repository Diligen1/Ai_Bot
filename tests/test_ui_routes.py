from fastapi.testclient import TestClient

from app.server import app

client = TestClient(app)


def test_home_returns_200() -> None:
    response = client.get('/')
    assert response.status_code == 200
    assert 'Главная' in response.text


def test_market_returns_200() -> None:
    response = client.get('/market')
    assert response.status_code == 200
    assert 'Рынок' in response.text


def test_trades_returns_200() -> None:
    response = client.get('/trades')
    assert response.status_code == 200
    assert 'Сделки' in response.text


def test_analytics_returns_200() -> None:
    response = client.get('/analytics')
    assert response.status_code == 200
    assert 'Аналитика' in response.text


def test_brain_returns_200() -> None:
    response = client.get('/brain')
    assert response.status_code == 200
    assert 'МОЗГ ИИ' in response.text


def test_symbols_returns_200() -> None:
    response = client.get('/symbols')
    assert response.status_code == 200
    assert 'ТОРГОВЫЕ МОНЕТЫ' in response.text


def test_market_and_brain_render_unknown_and_missing_enum_values() -> None:
    response = client.get('/market')
    assert response.status_code == 200
    assert '—' not in response.text or 'Ошибка' not in response.text

    response = client.get('/brain')
    assert response.status_code == 200
    assert 'НЕ ОПРЕДЕЛЕНА' in response.text or 'НЕЙТРАЛЬНО' in response.text


def test_market_unknown_enum_does_not_500(monkeypatch) -> None:
    from app import server

    def fake_market_data() -> dict[str, dict[str, object]]:
        return {
            'BTCUSDT': {
                'last_price': 50000,
                'mark_price': 50010,
                'price_change_percent': 0.5,
                'volume': 1000,
                'open_interest': 2000,
                'funding_rate': 0.0001,
                'best_bid': 49990,
                'best_ask': 50020,
                'spread': 30,
                'status': 'ERROR',
                'last_update': '2026-08-12 12:00:00',
                'trend_4h': 'UNKNOWN',
                'trend_1h': None,
                'trend_15m': 'TEST_UNKNOWN_VALUE',
                'market_regime': 'TEST_UNKNOWN_VALUE',
                'direction': None,
                'technical_score': None,
                'trade_candidate': None,
            }
        }

    monkeypatch.setattr(server, 'get_market_data', fake_market_data)
    response = client.get('/market')
    assert response.status_code == 200
    assert 'TEST_UNKNOWN_VALUE' in response.text


def test_brain_missing_analysis_does_not_500(monkeypatch) -> None:
    from app import server

    def fake_brain_view(symbol: str | None = None) -> dict[str, object]:
        return {
            'symbol': 'BTCUSDT',
            'current_price': '-',
            'market_regime': 'TEST_UNKNOWN_VALUE',
            'direction': 'TEST_UNKNOWN_VALUE',
            'technical_score': '-',
            'confidence': 'UNKNOWN',
            'timeframe_alignment': 'UNKNOWN',
            'trade_candidate': None,
            'reasons': [],
            'block_reasons': [],
            'analysis_4h': {'trend': None, 'indicators': {}},
            'analysis_1h': {'trend': None, 'indicators': {}},
            'analysis_15m': {'trend': None, 'indicators': {}},
            'volume_current': '-',
            'volume_average': '-',
            'volume_ratio': '-',
            'atr': '-',
            'atr_percent': '-',
        }

    monkeypatch.setattr(server, 'get_brain_view', fake_brain_view)
    monkeypatch.setattr(server, 'get_market_symbols', lambda: ['BTCUSDT'])
    response = client.get('/brain')
    assert response.status_code == 200
    assert 'TEST_UNKNOWN_VALUE' in response.text
    assert 'МОЗГ ИИ' in response.text


def test_brain_missing_risk_decision_key_does_not_500(monkeypatch) -> None:
    from app import server

    def fake_brain_view(symbol: str | None = None) -> dict[str, object]:
        return {
            'symbol': 'BTCUSDT', 'current_price': '-', 'market_regime': 'UNKNOWN',
            'direction': 'NEUTRAL', 'technical_score': '-', 'confidence': 'UNKNOWN',
            'timeframe_alignment': 'UNKNOWN', 'trade_candidate': None,
            'reasons': [], 'block_reasons': [],
            'analysis_4h': {'trend': None, 'indicators': {}},
            'analysis_1h': {'trend': None, 'indicators': {}},
            'analysis_15m': {'trend': None, 'indicators': {}},
            'volume_current': '-', 'volume_average': '-', 'volume_ratio': '-',
            'atr': '-', 'atr_percent': '-',
            # no 'risk_decision' key at all (old-format data)
        }

    monkeypatch.setattr(server, 'get_brain_view', fake_brain_view)
    monkeypatch.setattr(server, 'get_market_symbols', lambda: ['BTCUSDT'])
    response = client.get('/brain')
    assert response.status_code == 200
    assert 'МОЗГ ИИ' in response.text


def test_dashboard_shows_paper_trading_disabled_by_default() -> None:
    response = client.get('/')
    assert response.status_code == 200
    assert 'PAPER TRADING' in response.text
    assert 'ВЫКЛ' in response.text


def test_paper_trading_toggle_enable_and_disable(monkeypatch) -> None:
    from app import server

    state = {'enabled': False}
    monkeypatch.setattr(server, 'set_paper_trading_enabled', lambda value: state.__setitem__('enabled', value))
    monkeypatch.setattr(server, 'get_paper_trading_status', lambda: {'enabled': state['enabled'], 'label': 'ВКЛ' if state['enabled'] else 'ВЫКЛ'})

    response = client.post('/paper-trading/toggle', data={'enabled': 'true'})
    assert response.status_code in (200, 303)
    assert state['enabled'] is True
    dashboard = client.get('/')
    assert 'ВКЛ' in dashboard.text

    response = client.post('/paper-trading/toggle', data={'enabled': 'false'})
    assert response.status_code in (200, 303)
    assert state['enabled'] is False
    dashboard = client.get('/')
    assert 'ВЫКЛ' in dashboard.text


def test_trades_source_filter_variants_return_200() -> None:
    for url in ('/trades', '/trades?source=paper', '/trades?source=seed', '/trades?source=bogus'):
        response = client.get(url)
        assert response.status_code == 200
        assert 'Сделки' in response.text


def test_brain_risk_decision_none_does_not_500(monkeypatch) -> None:
    from app import server

    def fake_brain_view(symbol: str | None = None) -> dict[str, object]:
        return {
            'symbol': 'BTCUSDT', 'current_price': '-', 'market_regime': 'UNKNOWN',
            'direction': 'NEUTRAL', 'technical_score': '-', 'confidence': 'UNKNOWN',
            'timeframe_alignment': 'UNKNOWN', 'trade_candidate': None,
            'reasons': [], 'block_reasons': [],
            'analysis_4h': {'trend': None, 'indicators': {}},
            'analysis_1h': {'trend': None, 'indicators': {}},
            'analysis_15m': {'trend': None, 'indicators': {}},
            'volume_current': '-', 'volume_average': '-', 'volume_ratio': '-',
            'atr': '-', 'atr_percent': '-',
            'risk_decision': None,
        }

    monkeypatch.setattr(server, 'get_brain_view', fake_brain_view)
    monkeypatch.setattr(server, 'get_market_symbols', lambda: ['BTCUSDT'])
    response = client.get('/brain')
    assert response.status_code == 200
    assert 'МОЗГ ИИ' in response.text


def test_profit_split_settings_update_via_post(monkeypatch) -> None:
    from app import server

    state = {'spot': 50.0, 'futures': 50.0}

    def fake_set(spot_percent: float, futures_percent: float) -> None:
        if spot_percent < 0 or futures_percent < 0:
            raise ValueError('Проценты не могут быть отрицательными')
        if abs((spot_percent + futures_percent) - 100.0) > 1e-6:
            raise ValueError('Сумма процентов Spot и Futures должна быть равна 100%')
        state['spot'] = spot_percent
        state['futures'] = futures_percent

    def fake_view() -> dict[str, object]:
        return {
            'spot_percent': f"{state['spot']:.0f}%",
            'futures_percent': f"{state['futures']:.0f}%",
            'spot_percent_raw': state['spot'],
            'futures_percent_raw': state['futures'],
            'presets': [],
        }

    monkeypatch.setattr(server, 'set_profit_split_settings', fake_set)
    monkeypatch.setattr(server, 'get_profit_split_settings_view', fake_view)

    response = client.post('/profit-split/settings', data={'spot_percent': '30', 'futures_percent': '70'})
    assert response.status_code in (200, 303)
    assert state == {'spot': 30.0, 'futures': 70.0}
    dashboard = client.get('/')
    assert '30%' in dashboard.text
    assert '70%' in dashboard.text


def test_profit_split_invalid_sum_shows_russian_error(monkeypatch) -> None:
    from app import server

    def fake_set(spot_percent: float, futures_percent: float) -> None:
        raise ValueError('Сумма процентов Spot и Futures должна быть равна 100%')

    monkeypatch.setattr(server, 'set_profit_split_settings', fake_set)

    response = client.post(
        '/profit-split/settings',
        data={'spot_percent': '40', 'futures_percent': '70'},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert 'Сумма процентов' in response.text


def test_profit_split_negative_percent_shows_russian_error(monkeypatch) -> None:
    from app import server

    def fake_set(spot_percent: float, futures_percent: float) -> None:
        raise ValueError('Проценты не могут быть отрицательными')

    monkeypatch.setattr(server, 'set_profit_split_settings', fake_set)

    response = client.post(
        '/profit-split/settings',
        data={'spot_percent': '-10', 'futures_percent': '110'},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert 'отрицательными' in response.text
