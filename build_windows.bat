@echo off
REM Construit ClimatInterieur.exe a partir de app.py.
REM A executer sur une machine Windows avec Python installe (python.org, "Add to PATH" coche).

setlocal

if not exist .venv (
    py -m venv .venv
)

call .venv\Scripts\activate.bat

pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

pyinstaller --onefile --name ClimatInterieur app.py

if exist .env (
    copy /Y .env dist\.env >nul
) else (
    copy /Y .env.example dist\.env >nul
    echo ATTENTION : dist\.env est vide, remplis MQTT_USERNAME et MQTT_PASSWORD dedans avant de lancer l'exe.
)

echo.
echo Executable pret : dist\ClimatInterieur.exe (avec son .env a cote)
echo Copie tout le contenu de dist\ ensemble, puis double-clique ClimatInterieur.exe.
echo Le navigateur s'ouvre automatiquement.
echo.

endlocal
