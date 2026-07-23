@echo off
REM ============================================================================
REM  Maescar backend - modo LOCAL (desarrollo)
REM  Usa los valores de .env tal cual (DEBUG=True, localhost).
REM  Empareja con 'npm run dev' en el frontend  ->  http://localhost:5173
REM  Para APAGAR: pulsa Ctrl+C en esta ventana (o cierrala).
REM ============================================================================
cd /d "%~dp0"

echo.
echo  === BACKEND EN MODO LOCAL (http://127.0.0.1:8000) ===
echo  Frontend: abre otra terminal en 'frontend' y ejecuta  npm run dev
echo.

".venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000
