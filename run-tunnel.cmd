@echo off
REM ============================================================================
REM  Maescar backend - modo TUNNEL (produccion / sitio publico)
REM  Sirve el backend con los valores de PRODUCCION y abre el Cloudflare Tunnel.
REM  Frontend publico:  https://imaescar.xyz  (lo sirve frontend\run-tunnel.cmd,
REM  por el mismo tunnel; https://maescar.pages.dev sigue funcionando tambien)
REM  Para APAGAR: Ctrl+C aqui, y cierra la ventana del tunnel.
REM ============================================================================
cd /d "%~dp0"

REM --- Sobrescribe SOLO los 4 valores que difieren de .env (que queda en dev) ---
set DJANGO_DEBUG=False
set DJANGO_ALLOWED_HOSTS=api.api-maescar123.xyz
set CORS_ALLOWED_ORIGINS=https://imaescar.xyz,https://www.imaescar.xyz,https://maescar.pages.dev
set FRONTEND_BASE_URL=https://imaescar.xyz

REM --- Abre el Cloudflare Tunnel en su propia ventana ---
set "CLOUDFLARED=C:\Program Files (x86)\cloudflared\cloudflared.exe"
if exist "%CLOUDFLARED%" (
    start "Cloudflare Tunnel (maescar)" "%CLOUDFLARED%" tunnel run maescar
) else (
    echo [AVISO] No encontre cloudflared en "%CLOUDFLARED%".
    echo         Abre otra terminal y ejecuta manualmente:  cloudflared tunnel run maescar
)

echo.
echo  === BACKEND EN MODO TUNNEL (publico via https://api.api-maescar123.xyz) ===
echo  Frontend publico: https://imaescar.xyz  (levantalo con frontend\run-tunnel.cmd)
echo.

REM --- DEBUG=False necesita --insecure para servir el static del admin ---
".venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000 --insecure
