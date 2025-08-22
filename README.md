# Bond Analyzer - Analizador de Bonos Argentinos

Este nace como respuesta al este tweet: https://x.com/JohnGalt_is_www/status/1958870754872734161

Herramienta CLI completa para analizar bonos duales argentinos, calculando TEM de prospecto y mercado con integración en tiempo real de datos del BCRA.

## Features
- 🏛️ **Integración BCRA**: Datos TAMAR en tiempo real
- 📊 **Análisis Dual**: TEM prospecto vs mercado
- 🔄 **Auto-fetch**: Precios y TIREA automáticos
- 📈 **Conversiones**: TAMAR→TEM, TIREA→TEM
- 🗓️ **Días hábiles**: Manejo feriados argentinos

## Setup
1. Activar virtualenv: `source venv/bin/activate`
2. Instalar dependencias: `pip install -r requirements.txt`
3. Para notebooks: `jupyter lab` (opcional)

## Uso
**Comando principal:**
```bash
python bond_analyzer.py --ticker TTM26
```

**Ejemplos avanzados:**
```bash
# Con TAMAR específico
python bond_analyzer.py --ticker TTM26 --tamar-id 44 --market-source tamar

# Con overrides manuales  
python bond_analyzer.py --ticker TTM26 --price 114.5 --tirea 0.3055

# 📊 Notebook interactivo (recomendado para análisis visual)
jupyter lab jupyter/bond_analyzer_demo.ipynb
```

## 📊 Análisis Visual
El **notebook interactivo** incluye:
- 📈 **Gráficos TAMAR históricos**
- 📊 **Comparaciones visuales** prospecto vs mercado  
- 🎮 **Análisis interactivo** de todos los bonos duales
- 📋 **Explicaciones paso a paso**

## Notas
- Prospecto duales: TEM = max(TEM fija mensual; TAMAR TEM), TAMAR promedio en [emisión-10 dh, vencimiento-10 dh].
- Mercado: si hay TIREA, TEM = (1+TIREA)^(1/12)-1; si no, desde precio asumiendo bullet y 30E/360.
- Para duales conocidos (`TTM26`, `TTJ26`, `TTS26`, `TTD26`) se usa fallback de vencimiento y TIREA desde la publicación oficial (Presidencia/Argentina.gob.ar): `https://www.argentina.gob.ar/noticias/llamado-licitacion-para-la-conversion-de-titulos-elegibles-por-una-canasta-de-0`.


Fuentes:
- APIS BCRA con datos historicos de TAMAR: https://bcra.gob.ar/BCRAyVos/catalogo-de-APIs-banco-central.asp
- API Milton Casco para precios bonos: https://data912.com