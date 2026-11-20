from __future__ import annotations

from hunter.collect.technical import parse_show_technical


def test_parse_show_technical_table_format():
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Техническая информация</title></head>
    <body>
        <table>
            <tr><th>Ай-пи адрес:</th><td>185.220.101.5</td></tr>
            <tr><th>Уровень доверия:</th><td>Высокий</td></tr>
            <tr><th>Ваш город:</th><td>Москва</td></tr>
            <tr><th>Ваш регион:</th><td>MOW</td></tr>
            <tr><th>Куки (Cookies):</th><td>Включены</td></tr>
            <tr><th>Сервер:</th><td>avito-site-v2150-prod-k8s-01</td></tr>
            <tr><th>User-Agent:</th><td>Mozilla/5.0 Chrome/133.0</td></tr>
        </table>
    </body>
    </html>
    """
    info = parse_show_technical(html)
    assert info.ip == "185.220.101.5"
    assert info.trust_level == "Высокий"
    assert info.city == "Москва"
    assert info.region == "MOW"
    assert info.cookies_enabled is True
    assert info.server == "avito-site-v2150-prod-k8s-01"
    assert info.is_trusted is True


def test_parse_show_technical_text_dl_format():
    html = """
    <div>
        <dl>
            <dt>IP address</dt><dd>95.173.136.18</dd>
            <dt>Trust Level</dt><dd>Низкий (подозрительная активность)</dd>
            <dt>Город</dt><dd>Санкт-Петербург</dd>
            <dt>Регион</dt><dd>SPE</dd>
            <dt>Cookies</dt><dd>Отключены</dd>
            <dt>Нода</dt><dd>avito-edge-44</dd>
        </dl>
    </div>
    """
    info = parse_show_technical(html)
    assert info.ip == "95.173.136.18"
    assert "Низкий" in (info.trust_level or "")
    assert info.city == "Санкт-Петербург"
    assert info.region == "SPE"
    assert info.cookies_enabled is False
    assert info.server == "avito-edge-44"
    assert info.is_trusted is False


def test_parse_show_technical_empty_or_malformed():
    info = parse_show_technical("")
    assert info.ip is None
    assert info.trust_level is None
    assert info.is_trusted is True
