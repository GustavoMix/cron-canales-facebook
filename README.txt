FACEBOOK LIVE BOLIVIA - PRUEBA LOCAL

1. Ejecuta instalar.bat
2. Prueba primero probar_red_uno.bat
3. Luego probar_unitel.bat
4. Para revisar todas las fuentes de fuentes.json: revisar_todas_una_vez.bat
5. Para repetir de forma conservadora: vigilar_cada_15_minutos.bat

En GitHub Actions, fuentes.json se reparte en 4 grupos que corren en paralelo
cada hora (ver GITHUB_ACTIONS.txt) para no exceder el timeout del job ni
mandar de golpe todas las requests a Facebook.

Archivos de salida:
- resultado_facebook_bolivia.json: todas las fuentes
- lives_bolivia.json: solamente transmisiones detectadas como activas

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
