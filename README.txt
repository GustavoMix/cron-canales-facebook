FACEBOOK LIVE BOLIVIA - PRUEBA LOCAL

1. Ejecuta instalar.bat
2. Prueba primero probar_red_uno.bat
3. Luego probar_unitel.bat
4. Para revisar todas las fuentes de fuentes.json: revisar_todas_una_vez.bat
5. Para repetir de forma conservadora: vigilar_cada_15_minutos.bat

En GitHub Actions, fuentes.json se reparte en 9 grupos que corren escalonados
cada 30 minutos (ver GITHUB_ACTIONS.txt) para no exceder el timeout del job ni
mandar de golpe todas las requests a Facebook. Todas las fuentes son de
Facebook (se sacó el detector de YouTube que había, para no mezclar
plataformas).

El reparto lo calcula el script solo (--grupo N --de 9): para agregar canales
alcanza con editar fuentes.json, los workflows no se tocan. Los grupos son
partes iguales de la lista, sin significado temático; para agrupar por tema se
usan los campos "pais" y "categoria" del resultado combinado.

Archivos de salida:
- resultado_facebook_bolivia.json: todas las fuentes
- lives_bolivia.json: solamente transmisiones detectadas como activas
- salud_fuentes.json: qué fuentes responden y cuáles no (para detectar URLs
  muertas o mal escritas). Para quitar las muertas:
  py facebook_live_bolivia.py --podar --min-fallas 6
- auditoria_deteccion.json: mide de qué señal depende la detección y qué otros
  marcadores acompañan a las transmisiones confirmadas (ver GITHUB_ACTIONS.txt).
  Hoy TODAS las detecciones vienen de una sola señal; esto sirve para encontrar
  una segunda con datos reales.

Puedes editar fuentes.json y agregar cualquier URL:
- https://www.facebook.com/usuario
- https://www.facebook.com/profile.php?id=123456789
- https://www.facebook.com/groups/123456789 (experimental)

Ejemplos:
py facebook_live_bolivia.py --source "Bolivision" --visible
py facebook_live_bolivia.py --url "https://www.facebook.com/VictorHugoBolivia" --visible
py facebook_live_bolivia.py --limit 5

IMPORTANTE:
Este detector usa el navegador público y NO inicia sesión. Facebook puede cambiar
su HTML, pedir login o bloquear temporalmente automatizaciones. No extrae m3u8,
no descarga el video y no evade restricciones. Para producción, usar APIs oficiales
cuando sea posible y dejar este mecanismo como respaldo.
