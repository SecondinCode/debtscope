@echo off
title DebtScope
cd /d "%~dp0"

if not exist "%~dp0debtscope\gui.py" (
    echo.
    echo  Non trovo i file del programma accanto a DebtScope.bat.
    echo  Probabilmente lo hai aperto da DENTRO lo zip.
    echo.
    echo  Chiudi questa finestra, apri lo zip, clicca "Extract all" / "Estrai tutto",
    echo  e lancia DebtScope.bat dalla cartella estratta.
    echo.
    pause
    exit /b 1
)

where py >nul 2>nul
if errorlevel 1 (
    echo Python non trovato. Installalo da python.org e riprova.
    pause
    exit /b 1
)

py -c "import requests, bs4, openpyxl" >nul 2>nul
if errorlevel 1 (
    echo Prima apertura: installo le librerie necessarie, un minuto...
    py -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Installazione non riuscita. Manda una foto di questa finestra.
        pause
        exit /b 1
    )
)

rem Icona "DebtScope" sul Desktop, aggiornata ogni volta a questa cartella.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$l = Join-Path ([Environment]::GetFolderPath('Desktop')) 'DebtScope.lnk'; $s = (New-Object -ComObject WScript.Shell).CreateShortcut($l); $s.TargetPath = '%~dp0DebtScope.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Save()" >nul 2>nul

py -m debtscope.gui
if errorlevel 1 (
    echo.
    echo Qualcosa e' andato storto. Manda una foto di questa finestra.
    pause
)
