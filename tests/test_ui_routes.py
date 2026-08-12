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
