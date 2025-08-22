import argparse
import dataclasses
import datetime as dt
import json
import math
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
try:
    import urllib3  # type: ignore
    from urllib3.exceptions import InsecureRequestWarning  # type: ignore
    urllib3.disable_warnings(InsecureRequestWarning)
except Exception:
    pass
try:
    # Prefer system certificates where available (python-truststore)
    import truststore as _truststore  # type: ignore
    _truststore.inject_into_ssl()
except Exception:
    pass

try:
    import holidays  # type: ignore
except Exception:
    holidays = None  # type: ignore


BCRA_MONETARY_BASE_URL = "https://api.bcra.gob.ar/estadisticas/v3.0/monetarias"
DATA912_ENDPOINTS = [
    ("arg_bonds", "https://data912.com/live/arg_bonds"),
    ("arg_notes", "https://data912.com/live/arg_notes"),
]


# Known Duals fixed monthly TEM (prospectus), canonical maturity and reference TIREA
# Source (Presidencia/Argentina.gob.ar):
# https://www.argentina.gob.ar/noticias/llamado-licitacion-para-la-conversion-de-titulos-elegibles-por-una-canasta-de-0
DUAL_BONDS_MAP: Dict[str, Dict[str, Any]] = {
    "TTM26": {
        "fixed_monthly_tem": 0.0225,
        "maturity": dt.date(2026, 3, 16),
        "issue": dt.date(2025, 1, 29),
        "tirea_decimal": 0.3055,
    },
    "TTJ26": {
        "fixed_monthly_tem": 0.0219,
        "maturity": dt.date(2026, 6, 30),
        "issue": dt.date(2025, 1, 29),
        "tirea_decimal": 0.2965,
    },
    "TTS26": {
        "fixed_monthly_tem": 0.0217,
        "maturity": dt.date(2026, 9, 15),
        "issue": dt.date(2025, 1, 29),
        "tirea_decimal": 0.2931,
    },
    "TTD26": {
        "fixed_monthly_tem": 0.0214,
        "maturity": dt.date(2026, 12, 15),
        "issue": dt.date(2025, 1, 29),
        "tirea_decimal": 0.2893,
    },
}


@dataclasses.dataclass
class DualProspectusParams:
    issue_date: dt.date
    maturity_date: dt.date
    fixed_monthly_tem: float


def parse_date(date_str: str) -> dt.date:
    return dt.date.fromisoformat(date_str)


def days_30e_360(start_date: dt.date, end_date: dt.date) -> int:
    d1 = min(start_date.day, 30)
    d2 = min(end_date.day, 30)
    return (
        (end_date.year - start_date.year) * 360
        + (end_date.month - start_date.month) * 30
        + (d2 - d1)
    )


def year_fraction_30e_360(start_date: dt.date, end_date: dt.date) -> float:
    return days_30e_360(start_date, end_date) / 360.0


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
    if date_obj.weekday() >= 5:
        return False
    if ar_holidays is not None and date_obj in ar_holidays:
        return False
    return True


def shift_business_days(date_obj: dt.date, offset_days: int, use_holidays: bool = True) -> dt.date:
    if offset_days == 0:
        return date_obj
    step = 1 if offset_days > 0 else -1
    remaining = abs(offset_days)
    ar_h = build_ar_holidays(range(date_obj.year - 1, date_obj.year + 4)) if use_holidays else None
    current = date_obj
    while remaining > 0:
        current = current + dt.timedelta(days=step)
        if is_business_day(current, ar_h):
            remaining -= 1
    return current


def http_get_json(url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    try:
        resp = requests.get(url, headers=headers or {}, params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.SSLError:
        # Controlled fallback if TLS chain cannot be verified in this environment
        try:
            resp = requests.get(url, headers=headers or {}, params=params or {}, timeout=30, verify=False)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None
    except Exception:
        return None


def find_tamar_variable_id() -> Optional[int]:
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

    best: Optional[Tuple[int, Tuple[int, int, int, int]]] = None
    for item in results:
        try:
            var_id = int(item.get("idVariable"))
            desc = str(item.get("descripcion") or "")
        except Exception:
            continue
        sc = score(desc)
        if best is None or sc > best[1]:
            best = (var_id, sc)
    if best is not None and best[1][0]:
        return best[0]
    return None


def fetch_tamar_series_average(
    id_variable: Optional[int],
    desde: dt.date,
    hasta: dt.date,
) -> Optional[float]:
    var_id = id_variable or find_tamar_variable_id()
    if var_id is None:
        return None
    url = f"{BCRA_MONETARY_BASE_URL}/{var_id}"
    params = {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "limit": 3000, "offset": 0}
    data = http_get_json(url, headers={"Accept-Language": "es-AR"}, params=params)
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    if not isinstance(results, list) or not results:
        return None
    valores: List[float] = []
    for row in results:
        try:
            valores.append(float(row.get("valor")))
        except Exception:
            continue
    if not valores:
        return None
    promedio_percent = sum(valores) / float(len(valores))
    return promedio_percent / 100.0


def fetch_tamar_series_values(
    id_variable: Optional[int],
    desde: dt.date,
    hasta: dt.date,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch TAMAR observations (date, percent) within [desde, hasta].

    Returns a list sorted by date ascending: [{"fecha": date, "valor_percent": float}]
    """
    var_id = id_variable or find_tamar_variable_id()
    if var_id is None:
        return None
    url = f"{BCRA_MONETARY_BASE_URL}/{var_id}"
    params = {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "limit": 3000, "offset": 0}
    data = http_get_json(url, headers={"Accept-Language": "es-AR"}, params=params)
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    if not isinstance(results, list) or not results:
        return None
    out: List[Dict[str, Any]] = []
    for row in results:
        try:
            fstr = str(row.get("fecha"))
            v = float(row.get("valor"))
            d = _parse_date_any(fstr) or dt.date.fromisoformat(fstr)
            out.append({"fecha": d, "valor_percent": v})
        except Exception:
            continue
    out.sort(key=lambda x: x["fecha"])  # ascending
    return out


def fetch_tamar_latest_decimal(id_variable: int) -> Optional[float]:
    """Fetch latest TAMAR value (percent) from monetarias list and return decimal.

    Uses the top-level monetarias endpoint and filters by idVariable.
    """
    data = http_get_json(BCRA_MONETARY_BASE_URL, headers={"Accept-Language": "es-AR"})
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    if not isinstance(results, list):
        return None
    for item in results:
        try:
            if int(item.get("idVariable")) == int(id_variable):
                v = float(item.get("valor"))
                return v / 100.0
        except Exception:
            continue
    return None


def tamar_to_tem_monthly(tamar_as_decimal: float) -> float:
    # Fórmula oficial del BCRA: TAMAR_TEM = [(1+TAMAR/((365/32)))^((365/32))]^((1/12))-1
    base = 365.0 / 32.0
    effective_annual = (1.0 + tamar_as_decimal / base) ** base
    return effective_annual ** (1.0 / 12.0) - 1.0


def compute_dual_prospectus_tem(
    params: DualProspectusParams,
    use_holidays: bool = True,
    tamar_id: Optional[int] = None,
    tamar_avg_override: Optional[float] = None,
) -> Dict[str, Any]:
    start_window = shift_business_days(params.issue_date, -10, use_holidays=use_holidays)
    end_window = shift_business_days(params.maturity_date, -10, use_holidays=use_holidays)
    # Clamp to not exceed today in the API query range
    today = dt.date.today()
    clamped_end = min(end_window, today)

    series_values: Optional[List[Dict[str, Any]]] = None

    if tamar_avg_override is not None:
        tamar_avg_decimal = tamar_avg_override
        tamar_source = "override"
    else:
        tamar_avg_decimal = fetch_tamar_series_average(tamar_id, start_window, clamped_end)
        series_values = fetch_tamar_series_values(tamar_id, start_window, clamped_end)
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
        "tamar_latest_percent": (series_values[-1]["valor_percent"] if series_values else None),
        "tamar_latest_tem": (
            tamar_to_tem_monthly((series_values[-1]["valor_percent"]) / 100.0) if series_values else None
        ),
        "tamar_last_5_percent": (
            [v["valor_percent"] for v in (series_values[-5:] if series_values else [])] if series_values else None
        ),
        "tamar_samples_count": (len(series_values) if series_values else 0),
        "tamar_sample_range": (
            {
                "from": series_values[0]["fecha"].isoformat(),
                "to": series_values[-1]["fecha"].isoformat(),
            }
            if series_values
            else None
        ),
    }


def tirea_to_tem_monthly(tirea_decimal: float) -> float:
    return (1.0 + tirea_decimal) ** (1.0 / 12.0) - 1.0


def compute_market_tem(
    maturity_date: dt.date,
    settlement_date: Optional[dt.date] = None,
    face_value: float = 100.0,
    price_per_100: Optional[float] = None,
    tirea_decimal: Optional[float] = None,
    tamar_latest_decimal: Optional[float] = None,
) -> Dict[str, Any]:
    settlement = settlement_date or dt.date.today()
    results: Dict[str, Any] = {
        "settlement_date": settlement.isoformat(),
        "maturity_date": maturity_date.isoformat(),
        "face_value": face_value,
    }
    if tirea_decimal is not None:
        results["tirea_decimal"] = tirea_decimal
        results["tem_from_tirea"] = tirea_to_tem_monthly(tirea_decimal)
    if price_per_100 is not None and price_per_100 > 0:
        t_years = year_fraction_30e_360(settlement, maturity_date)
        if t_years > 0:
            tirea_bullet = (face_value / price_per_100) ** (1.0 / t_years) - 1.0
            results["tirea_from_price_bullet"] = tirea_bullet
            results["tem_from_price_bullet"] = tirea_to_tem_monthly(max(-0.9999, tirea_bullet))
            results["price_per_100"] = price_per_100
    preferred = results.get("tem_from_tirea") or results.get("tem_from_price_bullet")
    if preferred is None and tamar_latest_decimal is not None:
        results["tem_from_tamar_latest"] = tamar_to_tem_monthly(tamar_latest_decimal)
        preferred = results["tem_from_tamar_latest"]
    results["tem_market"] = preferred
    if preferred is not None:
        percent = round(preferred * 100.0, 2)
        results["tem_market_percentage"] = percent
        results["tem_market_percentage_str"] = f"{percent:.2f}%"
    else:
        results["tem_market_percentage"] = None
        results["tem_market_percentage_str"] = None
    return results


def _walk(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _normalize_yield(val: float) -> float:
    # Heuristic: if value looks like percent (> 1.0), convert to decimal
    return val / 100.0 if abs(val) > 1.0 else val


def _parse_date_any(s: str) -> Optional[dt.date]:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def fetch_data912_snapshot(ticker: str) -> Dict[str, Any]:
    ticker_u = ticker.upper()
    snapshot: Dict[str, Any] = {"ticker": ticker_u}
    for name, url in DATA912_ENDPOINTS:
        data = http_get_json(url)
        if data is None:
            continue
        for obj in _walk(data):
            try:
                # Identify symbol fields
                sym = None
                for k in ("symbol", "ticker", "code", "name", "bond"):
                    if k in obj:
                        if ticker_u in str(obj[k]).upper():
                            sym = str(obj[k])
                            break
                if sym is None:
                    continue
                # Price candidates
                for k in ("last", "price", "close", "p", "c", "px"):
                    if k in obj and isinstance(obj[k], (int, float)):
                        snapshot.setdefault("price_per_100", float(obj[k]))
                        snapshot.setdefault("price_source", f"{name}.{k}")
                        break
                # Yield candidates
                for k in ("ytm", "yield", "tirea", "tir", "ear"):
                    if k in obj and isinstance(obj[k], (int, float)):
                        snapshot.setdefault("tirea_decimal", _normalize_yield(float(obj[k])))
                        snapshot.setdefault("tirea_source", f"{name}.{k}")
                        break
                # Face / par value candidates
                for k in ("face", "par", "vn"):
                    if k in obj and isinstance(obj[k], (int, float)):
                        snapshot.setdefault("face_value", float(obj[k]))
                        snapshot.setdefault("face_source", f"{name}.{k}")
                        break
                # Maturity candidates
                for k in ("maturity", "maturityDate", "due", "vencimiento", "vto"):
                    if k in obj:
                        md = _parse_date_any(str(obj[k]))
                        if md is not None:
                            snapshot.setdefault("maturity_date", md)
                            snapshot.setdefault("maturity_source", f"{name}.{k}")
                            break
            except Exception:
                continue
        if "price_per_100" in snapshot and "tirea_decimal" in snapshot and "maturity_date" in snapshot:
            break
    return snapshot


def resolve_dual_params_from_ticker(ticker: str) -> Optional[DualProspectusParams]:
    meta = DUAL_BONDS_MAP.get(ticker.upper())
    if not meta:
        return None
    return DualProspectusParams(
        issue_date=meta["issue"],
        maturity_date=meta["maturity"],
        fixed_monthly_tem=meta["fixed_monthly_tem"],
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Calcula TEM de prospecto (si aplica a duales conocidos) y TEM de mercado para un bono dado por ticker.\n"
            "- Mercado: usa TIREA/precio/maturity auto-fetched si no se provee."
        )
    )
    parser.add_argument("--ticker", required=True, help="Ticker del bono, p.ej. TTM26")
    parser.add_argument("--issue", default=None, help="Fecha de emisión (YYYY-MM-DD), override manual (duales)")
    parser.add_argument("--maturity", default=None, help="Fecha de vencimiento (YYYY-MM-DD), override manual")
    parser.add_argument("--fixed-tem", type=float, default=None, help="TEM fija mensual (duales)")
    parser.add_argument("--no-holidays", action="store_true", help="Ignorar feriados en ventana de -10dh (duales)")
    parser.add_argument("--tamar-id", type=int, default=44, help="ID variable TAMAR en BCRA (opcional)")
    parser.add_argument("--tamar-avg", type=float, default=None, help="Promedio TAMAR decimal override (opcional)")
    parser.add_argument("--settlement", default=None, help="Fecha de liquidación para mercado (YYYY-MM-DD). Default: hoy")
    parser.add_argument("--face", type=float, default=None, help="VNO base para precio (default según snapshot o 100)")
    parser.add_argument("--price", type=float, default=None, help="Precio por 100 (override)")
    parser.add_argument("--tirea", type=float, default=None, help="TIREA decimal (override)")
    parser.add_argument(
        "--market-source",
        choices=["ytm", "price", "tamar", "auto"],
        default="tamar",
        help="Fuente para TEM de mercado: ytm (TIREA), price (bullet), tamar (última TAMAR), auto (prioridad ytm>price>tamar)",
    )

    args = parser.parse_args(argv)

    # Autofetch snapshot
    snap = fetch_data912_snapshot(args.ticker)
    price = args.price if args.price is not None else snap.get("price_per_100")
    tirea = args.tirea if args.tirea is not None else snap.get("tirea_decimal")
    face = args.face if args.face is not None else snap.get("face_value", 100.0)

    maturity: Optional[dt.date]
    if args.maturity is not None:
        maturity = parse_date(args.maturity)
        maturity_source = "override"
    else:
        maturity = snap.get("maturity_date")
        maturity_source = snap.get("maturity_source", "unavailable")
        if maturity is None:
            # Fallback: if ticker is a known dual, use canonical maturity
            dual_meta = DUAL_BONDS_MAP.get(args.ticker.upper())
            if dual_meta and isinstance(dual_meta.get("maturity"), dt.date):
                maturity = dual_meta["maturity"]
                maturity_source = "known_dual_map"
            # Fallback TIREA if missing and dual known
            if tirea is None and dual_meta and isinstance(dual_meta.get("tirea_decimal"), float):
                tirea = float(dual_meta["tirea_decimal"])

    settlement = parse_date(args.settlement) if args.settlement else dt.date.today()

    # Market TEM
    market = {}
    try:
        if maturity is not None:
            tamar_latest_dec: Optional[float] = None
            # If user provided a TAMAR id (or we resolve one), fetch latest to use as live proxy if needed
            try:
                if args.tamar_id is not None:
                    tamar_latest_dec = fetch_tamar_latest_decimal(args.tamar_id)
            except Exception:
                tamar_latest_dec = None
            market = compute_market_tem(
                maturity_date=maturity,
                settlement_date=settlement,
                face_value=face,
                price_per_100=price,
                tirea_decimal=tirea,
                tamar_latest_decimal=tamar_latest_dec,
            )
            if args.price is None and "price_per_100" in snap:
                market.setdefault("price_source", snap.get("price_source"))
            if args.tirea is None and "tirea_decimal" in snap:
                market.setdefault("tirea_source", snap.get("tirea_source"))

            # Respect --market-source preference
            if args.market_source == "ytm":
                market["tem_market"] = market.get("tem_from_tirea")
            elif args.market_source == "price":
                market["tem_market"] = market.get("tem_from_price_bullet")
            elif args.market_source == "tamar":
                market["tem_market"] = (
                    market.get("tem_from_tamar_latest")
                    if "tem_from_tamar_latest" in market and market.get("tem_from_tamar_latest") is not None
                    else (tamar_to_tem_monthly(tamar_latest_dec) if tamar_latest_dec is not None else None)
                )
            else:
                # auto already chosen inside compute_market_tem
                pass

            # Recompute percentage fields to match the selected source
            tm = market.get("tem_market")
            if tm is not None:
                percent = round(tm * 100.0, 2)
                market["tem_market_percentage"] = percent
                market["tem_market_percentage_str"] = f"{percent:.2f}%"
            else:
                market["tem_market_percentage"] = None
                market["tem_market_percentage_str"] = None
        else:
            market = {"error": "No se pudo determinar la fecha de vencimiento"}
    except Exception as exc:
        market = {"error": f"Error calculando TEM de mercado: {exc}"}

    # Prospectus TEM for known duals
    prospecto: Optional[Dict[str, Any]] = None
    try:
        dual_params = resolve_dual_params_from_ticker(args.ticker)
        if dual_params is not None:
            issue = parse_date(args.issue) if args.issue else dual_params.issue_date
            mat = parse_date(args.maturity) if args.maturity else dual_params.maturity_date
            fixed = float(args.fixed_tem) if args.fixed_tem is not None else dual_params.fixed_monthly_tem
            prospecto = compute_dual_prospectus_tem(
                DualProspectusParams(issue_date=issue, maturity_date=mat, fixed_monthly_tem=fixed),
                use_holidays=(not args.no_holidays),
                tamar_id=args.tamar_id,
                tamar_avg_override=args.tamar_avg,
            )
    except Exception as exc:
        prospecto = {"error": f"Error calculando TEM de prospecto: {exc}"}

    output = {
        "inputs": {
            "ticker": args.ticker.upper(),
            "settlement_date": settlement.isoformat(),
            "maturity_date": maturity.isoformat() if maturity else None,
            "maturity_source": maturity_source,
            "face_value": face,
            "price_per_100": price,
            "price_source": snap.get("price_source") if args.price is None else "override",
            "tirea_decimal": tirea,
            "tirea_source": snap.get("tirea_source") if args.tirea is None else "override",
        },
        "prospecto": prospecto,
        "mercado": market,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())


