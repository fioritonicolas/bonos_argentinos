import argparse
import dataclasses
import datetime as dt
import json
import math
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

try:
    import holidays  # type: ignore
except Exception as exc:  # pragma: no cover - optional at runtime
    holidays = None  # type: ignore


BCRA_MONETARY_BASE_URL = "https://api.bcra.gob.ar/estadisticas/v3.0/monetarias"
BCRA_FX_BASE_URL = "https://api.bcra.gob.ar/estadisticascambiarias/v1.0"
DATA912_BONDS_URL = "https://data912.com/live/arg_bonds"


@dataclasses.dataclass
class ProspectusParams:
    issue_date: dt.date = dt.date(2025, 1, 29)
    maturity_date: dt.date = dt.date(2026, 3, 16)
    fixed_monthly_tem: float = 0.0225  # 2.25% mensual


def parse_date(date_str: str) -> dt.date:
    return dt.date.fromisoformat(date_str)


def days_30e_360(start_date: dt.date, end_date: dt.date) -> int:
    """30E/360 day count convention.

    Treats all months as 30 days; years as 360 days. Reference: Eurobond basis.
    """
    d1 = min(start_date.day, 30)
    d2 = min(end_date.day, 30)
    return (
        (end_date.year - start_date.year) * 360
        + (end_date.month - start_date.month) * 30
        + (d2 - d1)
    )


def year_fraction_30e_360(start_date: dt.date, end_date: dt.date) -> float:
    return days_30e_360(start_date, end_date) / 360.0


def months_fraction_30e_360(start_date: dt.date, end_date: dt.date) -> float:
    # Per prospectus: exponent used is (DIAS/360) * 12
    return year_fraction_30e_360(start_date, end_date) * 12.0


def build_ar_holidays(years: Iterable[int]) -> Optional[set]:
    if holidays is None:
        return None
    try:
        ar = set()
        for y in years:
            for hday in holidays.country_holidays("AR", years=[y]).keys():  # type: ignore[attr-defined]
                ar.add(hday)
        return ar
    except Exception:
        return None


def is_business_day(date_obj: dt.date, ar_holidays: Optional[set]) -> bool:
    if date_obj.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    if ar_holidays is not None and date_obj in ar_holidays:
        return False
    return True


def shift_business_days(date_obj: dt.date, offset_days: int, use_holidays: bool = True) -> dt.date:
    if offset_days == 0:
        return date_obj
    step = 1 if offset_days > 0 else -1
    remaining = abs(offset_days)
    if use_holidays:
        years = range(date_obj.year - 1, date_obj.year + 4)
        ar_h = build_ar_holidays(years)
    else:
        ar_h = None
    current = date_obj
    while remaining > 0:
        current = current + dt.timedelta(days=step)
        if is_business_day(current, ar_h):
            remaining -= 1
    return current


def http_get_json(url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
    resp = requests.get(url, headers=headers or {}, params=params or {}, timeout=30)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        # Fallback: try to parse as JSON
        return json.loads(resp.text)


def find_tamar_variable_id() -> Optional[int]:
    """Search BCRA v3.0 monetary series for TAMAR promedio bancos privados.

    Returns the idVariable if found, else None.
    """
    url = BCRA_MONETARY_BASE_URL
    headers = {"Accept-Language": "es-AR"}
    data = http_get_json(url, headers=headers)
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    if not isinstance(results, list):
        return None

    def score(desc: str) -> Tuple[int, int, int, int]:
        dlow = desc.lower()
        return (
            ("tamar" in dlow),
            ("promedio" in dlow and "bancos" in dlow and "privados" in dlow),
            ("plazo fijo" in dlow),
            ("mil millones" in dlow) or ("1.000.000.000" in dlow) or ("1000000000" in dlow),
        )

    best_match: Optional[Tuple[int, Tuple[int, int, int, int], str]] = None
    for item in results:
        try:
            var_id = int(item.get("idVariable"))
            desc = str(item.get("descripcion") or "")
        except Exception:
            continue
        s = score(desc)
        # Higher tuple compares greater; we want the most specific match
        if best_match is None or s > best_match[1]:
            best_match = (var_id, s, desc)

    if best_match is not None and best_match[1][0]:  # must at least contain 'tamar'
        return best_match[0]
    return None


def fetch_tamar_series_average(
    id_variable: Optional[int],
    desde: dt.date,
    hasta: dt.date,
) -> Optional[float]:
    """Return arithmetic average of TAMAR values (as decimal, not percent) over [desde, hasta].

    If id_variable is None, tries to discover it first.
    """
    var_id = id_variable or find_tamar_variable_id()
    if var_id is None:
        return None

    url = f"{BCRA_MONETARY_BASE_URL}/{var_id}"
    params = {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "limit": 3000,
        "offset": 0,
    }
    data = http_get_json(url, params=params)
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    if not isinstance(results, list) or len(results) == 0:
        return None
    valores: List[float] = []
    for row in results:
        try:
            val = float(row.get("valor"))
            valores.append(val)
        except Exception:
            continue
    if not valores:
        return None
    promedio_percent = sum(valores) / float(len(valores))
    # Convert percent to decimal
    return promedio_percent / 100.0


def tamar_to_tem_monthly(tamar_as_decimal: float) -> float:
    """Convert TAMAR (annual-ish per prospectus) to monthly effective TEM per formula:

    TAMAR_TEM = [ (1 + TAMAR / (365/32))^(365/32) ]^(1/12) - 1
    where TAMAR is a decimal (e.g., 0.50 for 50%).
    """
    base = 365.0 / 32.0
    effective_annual = (1.0 + (tamar_as_decimal / base)) ** base
    return effective_annual ** (1.0 / 12.0) - 1.0


def compute_prospectus_tem(
    params: ProspectusParams,
    use_holidays: bool = True,
    tamar_id: Optional[int] = None,
    tamar_avg_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute TEM per prospectus: max(fixed 2.25%, TAMAR TEM).

    Returns dict with details.
    """
    start_window = shift_business_days(params.issue_date, -10, use_holidays=use_holidays)
    end_window = shift_business_days(params.maturity_date, -10, use_holidays=use_holidays)

    if tamar_avg_override is not None:
        tamar_avg_decimal = tamar_avg_override
        tamar_source = "override"
    else:
        tamar_avg_decimal = fetch_tamar_series_average(tamar_id, start_window, end_window)
        tamar_source = "bcra_api" if tamar_avg_decimal is not None else "unavailable"

    tamar_tem: Optional[float] = None
    if tamar_avg_decimal is not None:
        tamar_tem = tamar_to_tem_monthly(tamar_avg_decimal)

    fixed_tem = params.fixed_monthly_tem
    tem = max(fixed_tem, tamar_tem) if tamar_tem is not None else fixed_tem

    return {
        "issue_date": params.issue_date.isoformat(),
        "maturity_date": params.maturity_date.isoformat(),
        "window_start_10bd": start_window.isoformat(),
        "window_end_10bd": end_window.isoformat(),
        "tamar_avg_decimal": tamar_avg_decimal,
        "tamar_tem": tamar_tem,
        "fixed_tem": fixed_tem,
        "tem_prospectus": tem,
        "tamar_source": tamar_source,
    }


def fetch_data912_ttm26_price(symbol_hint: str = "TTM26") -> Optional[float]:
    """Try to fetch a last price for TTM26 from data912.

    Returns price per 100 if available. This endpoint is not documented; we attempt a best-effort parse.
    """
    try:
        data = http_get_json(DATA912_BONDS_URL)
    except Exception:
        return None
    # The API might return a dict or list. We scan recursively for dicts with any key resembling symbol and price.
    def walk(node: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(node, dict):
            yield node
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for obj in walk(data):
        try:
            # Identify symbol fields
            sym = None
            for k in ("symbol", "ticker", "code", "name", "bond"):
                if k in obj:
                    val = str(obj[k]).upper()
                    if symbol_hint.upper() in val:
                        sym = val
                        break
            if sym is None:
                continue
            # Identify price-like fields
            price = None
            for k in ("last", "price", "close", "p", "c", "px"):
                if k in obj:
                    pv = float(obj[k])
                    price = pv
                    break
            if price is not None and math.isfinite(price):
                candidates.append((price, obj))
        except Exception:
            continue

    if not candidates:
        return None
    # Return the first candidate's price
    return candidates[0][0]


def tirea_to_tem_monthly(tirea_decimal: float) -> float:
    return (1.0 + tirea_decimal) ** (1.0 / 12.0) - 1.0


def compute_market_tem(
    maturity_date: dt.date,
    settlement_date: Optional[dt.date] = None,
    face_value: float = 100.0,
    price_per_100: Optional[float] = None,
    tirea_decimal: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute market TEM in two possible ways:

    1) From TIREA: TEM = (1 + TIREA)^(1/12) - 1
    2) From price (YTM bullet assumption): TIREA = (Face/Price)^(1/t) - 1, with t = year fraction 30E/360

    Returns dict with whichever computations were possible.
    """
    settlement = settlement_date or dt.date.today()
    results: Dict[str, Any] = {
        "settlement_date": settlement.isoformat(),
        "maturity_date": maturity_date.isoformat(),
        "face_value": face_value,
    }

    if tirea_decimal is not None:
        tem_from_tirea = tirea_to_tem_monthly(tirea_decimal)
        results["tem_from_tirea"] = tem_from_tirea
        results["tirea_decimal"] = tirea_decimal

    if price_per_100 is not None and price_per_100 > 0:
        t_years = year_fraction_30e_360(settlement, maturity_date)
        if t_years <= 0:
            results["tem_from_price_bullet"] = None
        else:
            tirea_bullet = (face_value / price_per_100) ** (1.0 / t_years) - 1.0
            tem_from_price = tirea_to_tem_monthly(max(-0.9999, tirea_bullet))
            results["tem_from_price_bullet"] = tem_from_price
            results["tirea_from_price_bullet"] = tirea_bullet
            results["price_per_100"] = price_per_100

    # Preferred market TEM: from TIREA if available; else from price_bullet
    preferred = results.get("tem_from_tirea")
    if preferred is None:
        preferred = results.get("tem_from_price_bullet")
    results["tem_market"] = preferred
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Calcula la TEM de prospecto y de mercado para TTM26 (Bono Dual 16/03/2026).\n"
            "- Prospecto: max(2,25% mensual; TAMAR TEM) con ventana [emision-10dh, vencimiento-10dh].\n"
            "- Mercado: de TIREA si se indica; si no, desde precio bajo supuesto bullet."
        )
    )

    parser.add_argument("--issue", default="2025-01-29", help="Fecha de emisión (YYYY-MM-DD)")
    parser.add_argument("--maturity", default="2026-03-16", help="Fecha de vencimiento (YYYY-MM-DD)")
    parser.add_argument("--no-holidays", action="store_true", help="Ignorar feriados (sólo lun-vie)")
    parser.add_argument("--tamar-id", type=int, default=None, help="ID de variable TAMAR en BCRA (opcional)")
    parser.add_argument(
        "--tamar-avg", type=float, default=None,
        help="Promedio TAMAR como decimal (ej: 0.55 para 55%%) para override manual"
    )
    parser.add_argument("--fixed-tem", type=float, default=0.0225, help="TEM fija mensual del prospecto (default 0.0225)")

    parser.add_argument("--settlement", default=None, help="Fecha de liquidación/valoración p/mercado (YYYY-MM-DD). Default: hoy")
    parser.add_argument("--face", type=float, default=100.0, help="VNO base para precio (default 100)")
    parser.add_argument("--price", type=float, default=None, help="Precio de mercado por 100 (opcional)")
    parser.add_argument("--tirea", type=float, default=None, help="TIREA de mercado (decimal, ej: 0.3055)")
    parser.add_argument("--fetch-price", action="store_true", help="Intentar obtener precio de TTM26 desde data912")

    args = parser.parse_args(argv)

    issue = parse_date(args.issue)
    maturity = parse_date(args.maturity)
    fixed_tem = float(args.fixed_tem)
    params = ProspectusParams(issue_date=issue, maturity_date=maturity, fixed_monthly_tem=fixed_tem)

    try:
        prospecto = compute_prospectus_tem(
            params,
            use_holidays=(not args.no_holidays),
            tamar_id=args.tamar_id,
            tamar_avg_override=args.tamar_avg,
        )
    except Exception as exc:
        prospecto = {"error": f"Error calculando TEM de prospecto: {exc}"}

    settlement_date = parse_date(args.settlement) if args.settlement else None
    price = args.price
    if price is None and args.fetch_price:
        try:
            price = fetch_data912_ttm26_price()
        except Exception:
            price = None

    tirea = args.tirea
    try:
        mercado = compute_market_tem(
            maturity_date=maturity,
            settlement_date=settlement_date,
            face_value=args.face,
            price_per_100=price,
            tirea_decimal=tirea,
        )
    except Exception as exc:
        mercado = {"error": f"Error calculando TEM de mercado: {exc}"}

    output = {
        "prospecto": prospecto,
        "mercado": mercado,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
        sys.exit(main())


