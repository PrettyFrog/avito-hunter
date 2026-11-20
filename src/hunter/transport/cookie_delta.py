"""Scoped cookie updates and deletions carried through recovery persistence."""
from typing import Any


class CookieDelta(list):
    def __init__(self, cookies=(), deleted=()):
        super().__init__(cookies)
        self.deleted = frozenset(deleted)


def cookie_key(cookie: dict[str, Any]):
    return (cookie['name'], cookie.get('domain') or '.avito.ru', cookie.get('path') or '/')


def export_cookies(client):
    source = client.cookies
    if isinstance(source, dict):
        return [{'name': k, 'value': v, 'domain': '.avito.ru', 'path': '/'} for k, v in source.items()]
    result = []
    for c in source.jar:
        result.append({'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
                       'expires': c.expires, 'secure': c.secure,
                       'httpOnly': c.has_nonstandard_attr('HttpOnly'),
                       **({'sameSite': c.get_nonstandard_attr('SameSite')} if c.has_nonstandard_attr('SameSite') else {})})
    return result


def commit_staged_cookies(owner, stage):
    """Publish without an await; caller checks the original snapshot immediately before this."""
    final = export_cookies(stage)
    deleted = stage.initial_cookie_keys - {cookie_key(c) for c in final}
    if isinstance(owner.cookies, dict):
        owner.cookies = dict(stage.cookies)
    else:
        owner.cookies.jar = stage.cookies.jar
    return CookieDelta(final, deleted)
