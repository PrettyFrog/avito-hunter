from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_NON_CITY_SLUGS = frozenset({
    "all", "rossiya", "item", "items", "catalog", "brands", "user",
    "profile", "soobshestva", "favorites", "messages", "cart",
})

_AVITO_SLUG_MAP: dict[str, str] = {
    "moskva": "Москва", "sankt-peterburg": "Санкт-Петербург", "spb": "Санкт-Петербург",
    "novosibirsk": "Новосибирск", "ekaterinburg": "Екатеринбург", "kazan": "Казань",
    "nizhniy_novgorod": "Нижний Новгород", "chelyabinsk": "Челябинск",
    "krasnoyarsk": "Красноярск", "samara": "Самара", "ufa": "Уфа",
    "rostov-na-donu": "Ростов-на-Дону", "omsk": "Омск", "krasnodar": "Краснодар",
    "voronezh": "Воронеж", "perm": "Пермь", "volgograd": "Волгоград",
    "saratov": "Саратов", "tyumen": "Тюмень", "tolyatti": "Тольятти",
    "barnaul": "Барнаул", "izhevsk": "Ижевск", "mahachkala": "Махачкала",
    "habarovsk": "Хабаровск", "ulyanovsk": "Ульяновск", "irkutsk": "Иркутск",
    "vladivostok": "Владивосток", "yaroslavl": "Ярославль", "sevastopol": "Севастополь",
    "stavropol": "Ставрополь", "tomsk": "Томск", "kemerovo": "Кемерово",
    "naberezhnye_chelny": "Набережные Челны", "orenburg": "Оренбург",
    "novokuznetsk": "Новокузнецк", "balashiha": "Балашиха", "ryazan": "Рязань",
    "cheboksary": "Чебоксары", "penza": "Пенза", "lipetsk": "Липецк",
    "kaliningrad": "Калининград", "astrahan": "Астрахань", "tula": "Тула",
    "sochi": "Сочи", "kursk": "Курск", "arhangelsk": "Архангельск",
    "kirovskaya_oblast_kirov": "Киров", "kirov": "Киров", "ulan-ude": "Улан-Удэ",
    "tver": "Тверь", "magnitogorsk": "Магнитогорск", "surgut": "Сургут",
    "bryansk": "Брянск", "ivanovo": "Иваново", "yakutsk": "Якутск",
    "vladimir": "Владимир", "simferopol": "Симферополь", "belgorod": "Белгород",
    "nizhniy_tagil": "Нижний Тагил", "kaluga": "Калуга", "chita": "Чита",
    "groznyy": "Грозный", "volzhskiy": "Волжский", "smolensk": "Смоленск",
    "saransk": "Саранск", "cherepovets": "Череповец", "kurgan": "Курган",
    "vologda": "Вологда", "orel": "Орёл", "vladikavkaz": "Владикавказ",
    "podolsk": "Подольск", "murmansk": "Мурманск", "tambov": "Тамбов",
    "petrozavodsk": "Петрозаводск", "sterlitamak": "Стерлитамак",
    "kostroma": "Кострома", "novorossiysk": "Новороссийск", "yoshkar-ola": "Йошкар-Ола",
    "taganrog": "Таганрог", "komsomolsk-na-amure": "Комсомольск-на-Амуре",
    "syktyvkar": "Сыктывкар", "nalchik": "Нальчик", "nizhnekamsk": "Нижнекамск",
    "shahty": "Шахты", "dzerzhinsk": "Дзержинск", "bratsk": "Братск",
    "angarsk": "Ангарск", "engels": "Энгельс", "blagoveschensk": "Благовещенск",
    "staryy_oskol": "Старый Оскол", "velikiy_novgorod": "Великий Новгород",
    "korolev": "Королёв", "pskov": "Псков", "mytischi": "Мытищи",
    "biysk": "Бийск", "lyubertsy": "Люберцы", "prokopevsk": "Прокопьевск",
    "yuzhno-sahalinsk": "Южно-Сахалинск", "himki": "Химки", "elektrostal": "Электросталь",
    "kolomna": "Коломна", "odintsovo": "Одинцово", "krasnogorsk": "Красногорск",
    "domodedovo": "Домодедово", "schelkovo": "Щёлково", "serpuhov": "Серпухов",
    "ramenskoe": "Раменское", "orehovo-zuevo": "Орехово-Зуево", "dolgoprudnyy": "Долгопрудный",
    "pushkino": "Пушкино", "reutov": "Реутов", "zhukovskiy": "Жуковский",
    "noginsk": "Ногинск", "sergiev_posad": "Сергиев Посад", "vidnoe": "Видное",
    "voskresensk": "Воскресенск", "lobnya": "Лобня", "kolpino": "Колпино",
    "kronstadt": "Кронштадт", "petergof": "Петергоф", "pushkin": "Пушкин",
    "sestroretsk": "Сестрорецк", "krasnoye_selo": "Красное Село", "krasnoe_selo": "Красное Село",
    "zelenograd": "Зеленоград", "vyborg": "Выборг", "gatchina": "Гатчина",
    "sosnovyy_bor": "Сосновый Бор", "tihvin": "Тихвин", "kirishi": "Кириши",
    "kingisepp": "Кингисепп", "volhov": "Волхов", "vsevolozhsk": "Всеволожск",
    "tosno": "Тосно", "luga": "Луга", "slantsy": "Сланцы", "kirovsk": "Кировск",
    "otradnoe": "Отрадное", "pikalevo": "Пикалёво", "priozersk": "Приозерск",
    "shlisselburg": "Шлиссельбург", "murino": "Мурино", "pyatigorsk": "Пятигорск",
    "abakan": "Абакан", "yalta": "Ялта", "evpatoriya": "Евпатория",
    "kaspiysk": "Каспийск", "norilsk": "Норильск", "essentuki": "Ессентуки",
    "nizhnevartovsk": "Нижневартовск", "petropavlovsk-kamchatskiy": "Петропавловск-Камчатский",
    "obninsk": "Обнинск", "cherkessk": "Черкесск", "feodosiya": "Феодосия",
    "berdsk": "Бердск", "gorno-altaysk": "Горно-Алтайск", "eysk": "Ейск",
    "balakovo": "Балаково", "novyy_urengoy": "Новый Уренгой", "andreevka": "Андреевка",
    "salsk": "Сальск", "meleuz": "Мелеуз", "kurchaloy": "Курчалой", "bor": "Бор",
    "urus-martan": "Урус-Мартан", "kamensk-uralskiy": "Каменск-Уральский",
    "moskovskaya_oblast": "Московская область", "leningradskaya_oblast": "Ленинградская область",
}

_TRANSLIT_REV = (
    ("shch", "щ"), ("sch", "щ"), ("yo", "ё"), ("zh", "ж"), ("ch", "ч"),
    ("sh", "ш"), ("kh", "х"), ("ts", "ц"), ("yu", "ю"), ("ya", "я"),
    ("iy", "ий"), ("yy", "ый"), ("ey", "ей"), ("oy", "ой"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"),
    ("e", "е"), ("z", "з"), ("i", "и"), ("k", "к"), ("l", "л"),
    ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("u", "у"), ("f", "ф"), ("h", "х"),
    ("y", "ы"),
)


def _translit_word(w: str) -> str:
    out, i = [], 0
    while i < len(w):
        matched = False
        for lat, cyr in _TRANSLIT_REV:
            if w[i:].startswith(lat):
                out.append(cyr)
                i += len(lat)
                matched = True
                break
        if not matched:
            out.append(w[i])
            i += 1
    return "".join(out)


def _translit_slug(slug: str) -> str:
    res = []
    for word in slug.split("_"):
        sub_words = word.lower().split("-")
        res.append("-".join(_translit_word(sw).capitalize() for sw in sub_words))
    return " ".join(res)


def city_from_slug(slug: str) -> str | None:
    """Resolve an Avito URL slug to a human-readable city or region name."""
    s = slug.strip().lower()
    if not s or s in _NON_CITY_SLUGS:
        return None
    if s in _AVITO_SLUG_MAP:
        return _AVITO_SLUG_MAP[s]

    for prefix in ("sankt-peterburg", "moskva", "spb"):
        if s.startswith(prefix + "_"):
            sub = s[len(prefix) + 1:]
            main_city = _AVITO_SLUG_MAP.get(prefix, prefix.title())
            sub_name = _AVITO_SLUG_MAP.get(sub, _translit_slug(sub))
            return f"{main_city}, {sub_name}"

    # Handle regional composite slugs like 'volgogradskaya_oblast_volzhskiy'
    for suffix in sorted(_AVITO_SLUG_MAP.keys(), key=len, reverse=True):
        if len(suffix) > 3 and s.endswith("_" + suffix):
            return _AVITO_SLUG_MAP[suffix]

    # Fallback to smart reverse transliteration
    cand = _translit_slug(s)
    return cand if cand else None


def city_from_url(url_or_path: str) -> str | None:
    """Extract location city from an Avito URL or URL path segment."""
    if not url_or_path:
        return None
    path = url_or_path
    if "://" in path:
        path = urlparse(path).path
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return None
    return city_from_slug(parts[0])


def extract_item_city(node: dict[str, Any], url: str | None = None) -> str | None:
    """Multi-stage extractor for item location across JSON search nodes and MFE states."""
    loc = node.get("location") or node.get("geo")
    if isinstance(loc, dict):
        cand = loc.get("name") or loc.get("formattedAddress") or loc.get("cityName")
        if cand and str(cand).strip():
            return str(cand).strip()
    elif isinstance(loc, str) and loc.strip():
        return loc.strip()

    addr = node.get("addressDetailed")
    if isinstance(addr, dict):
        cand = addr.get("locationName") or addr.get("cityName") or addr.get("regionName")
        if cand and str(cand).strip():
            return str(cand).strip()
    elif isinstance(addr, str) and addr.strip():
        return addr.strip()

    if node.get("address") and isinstance(node["address"], str) and node["address"].strip():
        return node["address"].strip()

    url_target = url or node.get("urlPath") or node.get("url") or node.get("uri")
    if url_target and isinstance(url_target, str):
        city = city_from_url(url_target)
        if city:
            return city

    return None
