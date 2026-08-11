from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import parse_qs

from app.dashboard_data import (
    add_whitelist_symbol,
    get_analytics_data,
    get_ai_market_analysis,
    get_balance,
    get_brain_view,
    get_open_positions,
    get_risk_control,
    get_system_status,
    get_today_stats,
    get_trades_history,
    get_nav_items,
    get_whitelist_symbols,
    remove_whitelist_symbol,
)

app = FastAPI(title="AI Futures Bot Dashboard")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


def render_page(request: Request, template: str, context: dict) -> HTMLResponse:
    payload = {"request": request, **context, "nav_items": get_nav_items()}
    return templates.TemplateResponse(request, template, payload)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "dashboard.html",
        {
            "title": "Dashboard",
            "system_status": get_system_status(),
            "balance": get_balance(),
            "today": get_today_stats(),
            "risk_control": get_risk_control(),
            "open_positions": get_open_positions(),
            "ai_market_analysis": get_ai_market_analysis(),
        },
    )


@app.get("/trades", response_class=HTMLResponse)
def trades(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "trades.html",
        {
            "title": "Trades",
            "trades_history": get_trades_history(),
        },
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "analytics.html",
        {
            "title": "Analytics",
            "analytics": get_analytics_data(),
        },
    )


@app.get("/symbols", response_class=HTMLResponse)
def symbols(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "symbols.html",
        {
            "title": "Symbol Whitelist",
            "whitelist_symbols": get_whitelist_symbols(),
        },
    )


@app.post("/symbols")
async def symbols_submit(request: Request) -> RedirectResponse:
    body = await request.body()
    data = parse_qs(body.decode('utf-8'))
    symbol = data.get('symbol', [''])[0].strip().upper()
    action = data.get('action', ['add'])[0]
    if symbol:
        if action == 'remove':
            remove_whitelist_symbol(symbol)
        else:
            add_whitelist_symbol(symbol)
    return RedirectResponse(url='/symbols', status_code=303)


@app.get("/brain", response_class=HTMLResponse)
def brain(request: Request) -> HTMLResponse:
    return render_page(
        request,
        "brain.html",
        {
            "title": "AI Brain",
            "brain": get_brain_view(),
        },
    )
