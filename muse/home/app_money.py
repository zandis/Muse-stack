"""Money in the person's own currency, for the app (docs/spec/02-ux.md section 1, rule 7).

The router counts in US dollars and nothing here changes that. This module turns
dollar figures into `{usd, local, approx}` with decimal strings, using a rate the
person sets. There is no network lookup: the rate is a setting, and the reply
says so (`rate_source: "fixed setting"`) with the date it was set.

Settings (in the home's settings file, read through `Home.settings_env`):

  MUSE_CURRENCY             ISO code of the person's currency. Default TWD.
  MUSE_USD_RATE             how many units of it one US dollar is. Default 32.
  MUSE_USD_RATE_DATE        the day that rate was set (written by PATCH /v1/settings).
  MUSE_DAILY_PAID_CAP_USD   the router's own daily cap (muse/ration.py). Unset there means US$1.00.

The daily limit a new user sees. The owner's decision is NT$30. When
`MUSE_DAILY_PAID_CAP_USD` is unset and the currency is TWD, the home's limit is
30 units of local currency, i.e. US$0.9375 at the default rate. The home holds
its own paid requests (Ask) to that figure at once. The router's default stays
US$1.00 (`muse/ration.py` is unchanged), so `daily_limit.in_step` is false until
the converted figure has been written to the settings file, which the home does
the first time it stores a service's key through the API or the limit is saved
in Settings (`adopt_default_limit`). The router reads it at its next start.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping

DEFAULT_CURRENCY = "TWD"
DEFAULT_RATES = {"TWD": Decimal("32"), "USD": Decimal("1"), "JPY": Decimal("150"), "KRW": Decimal("1400")}
DEFAULT_RATE_DATE = "2026-11-13"  # the day the built-in rates above were written down
RATE_SOURCE = "fixed setting"
ZERO_DECIMALS = frozenset({"TWD", "JPY", "KRW"})  # shown without cents
CURRENCIES = ("TWD", "USD", "JPY", "KRW")
DEFAULT_LIMIT_LOCAL = {"TWD": Decimal("30")}  # the owner's decision: a new user sees NT$30
ROUTER_DEFAULT_USD = Decimal("1.00")  # muse/ration.py when MUSE_DAILY_PAID_CAP_USD is unset
MAX_RATE = Decimal("100000")
CAP_NAME = "MUSE_DAILY_PAID_CAP_USD"
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _dec(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def currency(env: Mapping[str, str]) -> str:
    raw = str(env.get("MUSE_CURRENCY", "") or "").strip().upper()
    return raw if raw in CURRENCIES else DEFAULT_CURRENCY


def rate(env: Mapping[str, str]) -> tuple[Decimal, str]:
    """(units of local currency per US dollar, the day it was set)."""
    code = currency(env)
    value = _dec(env.get("MUSE_USD_RATE", "")) if str(env.get("MUSE_USD_RATE", "") or "").strip() else None
    if code == "USD":
        return Decimal("1"), DEFAULT_RATE_DATE
    if value is None or value <= 0 or value > MAX_RATE:
        return DEFAULT_RATES[code], DEFAULT_RATE_DATE
    day = str(env.get("MUSE_USD_RATE_DATE", "") or "").strip()
    return value, day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else DEFAULT_RATE_DATE


def usd_text(value: Decimal) -> str:
    """A dollar amount as a decimal string: at least two places, at most four, no exponent."""
    q = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    text = f"{q:f}"
    while text.endswith("0") and len(text.split(".")[1]) > 2:
        text = text[:-1]
    return text


def local_text(value: Decimal, code: str) -> str:
    places = Decimal("1") if code in ZERO_DECIMALS else Decimal("0.01")
    return f"{value.quantize(places, rounding=ROUND_HALF_UP):f}"


def amount(usd: Any, env: Mapping[str, str]) -> dict[str, Any]:
    """`{usd, local, approx}` for a dollar figure. `approx` says the local figure was rounded from a conversion."""
    value = _dec(usd) or Decimal("0")
    per, _ = rate(env)
    code = currency(env)
    local = value * per
    shown = local_text(local, code)
    return {"usd": usd_text(value), "local": shown, "approx": code != "USD" and value != 0 and Decimal(shown) != local}


def router_cap_usd(env: Mapping[str, str]) -> Decimal:
    """What the router enforces for this environment: muse/ration.py's own reading of the setting."""
    from muse.ration import parse_paid_cap

    return parse_paid_cap(env.get(CAP_NAME))


def cap_is_set(env: Mapping[str, str]) -> bool:
    return bool(str(env.get(CAP_NAME, "") or "").strip())


def limit_usd(env: Mapping[str, str]) -> tuple[Decimal, str]:
    """(the home's daily limit in dollars, "setting" | "default")."""
    if cap_is_set(env):
        return router_cap_usd(env), "setting"
    code = currency(env)
    if code in DEFAULT_LIMIT_LOCAL:
        return DEFAULT_LIMIT_LOCAL[code] / rate(env)[0], "default"
    return ROUTER_DEFAULT_USD, "default"


def daily_limit(env: Mapping[str, str]) -> dict[str, Any]:
    usd, source = limit_usd(env)
    out = amount(usd, env)
    router = router_cap_usd(env)
    out.update(source=source, router_usd=usd_text(router), in_step=router == usd)
    return out


def money(env: Mapping[str, str], paid: Mapping[str, Any] | None) -> dict[str, Any]:
    """The `money` object of GET /v1/status."""
    per, day = rate(env)
    spent = (paid or {}).get("spent_usd", 0) or 0
    return {
        "currency": currency(env),
        "rate": f"{per.normalize():f}",
        "rate_source": RATE_SOURCE,
        "rate_date": day,
        "spent_today": amount(spent, env),
        "daily_limit": daily_limit(env),
    }


def local_to_usd(local: Any, env: Mapping[str, str]) -> Decimal | None:
    value = _dec(local)
    if value is None or value < 0:
        return None
    return value / rate(env)[0]


def cap_setting_text(usd: Decimal) -> str:
    """The dollar figure as it is written to the settings file: exact to six places, no exponent."""
    q = usd.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP).normalize()
    return f"{q:f}" if q != 0 else "0"
