# Bond TEM Calculator

Este nace como respuesta al este tweet: https://x.com/JohnGalt_is_www/status/1958870754872734161

CLI para calcular la TEM de prospecto (duales conocidos) y de mercado para bonos por ticker.

Setup
1) Activar virtualenv (este repo usa `venv`).
2) Instalar dependencias: `pip install -r requirements.txt`.

Uso
Ejemplos:
- Sólo con ticker (auto-fetch de precio/TIREA/maturity si hay datos):
  `python -m tasas.bond_tem --ticker TTM26`
- Con overrides manuales:
  `python -m tasas.bond_tem --ticker TTM26 --price 114.5 --tirea 0.3055`
- Aún funciona el wrapper anterior para TTM26:
  `python -m tasas.ttm26_tem`

Notas
- Prospecto duales: TEM = max(TEM fija mensual; TAMAR TEM), TAMAR promedio en [emisión-10 dh, vencimiento-10 dh].
- Mercado: si hay TIREA, TEM = (1+TIREA)^(1/12)-1; si no, desde precio asumiendo bullet y 30E/360.
- Para duales conocidos (`TTM26`, `TTJ26`, `TTS26`, `TTD26`) se usa fallback de vencimiento y TIREA desde la publicación oficial (Presidencia/Argentina.gob.ar): `https://www.argentina.gob.ar/noticias/llamado-licitacion-para-la-conversion-de-titulos-elegibles-por-una-canasta-de-0`.


Fuentes:
- APIS BCRA con datos historicos de TAMAR: https://bcra.gob.ar/BCRAyVos/catalogo-de-APIs-banco-central.asp
- API Milton Casco para precios bonos: https://data912.com