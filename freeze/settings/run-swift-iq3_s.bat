@echo off

title Strata swift-IQ3_S

cd /d "C:\dev\Strata"

"C:\dev\Strata\.venv\Scripts\python.exe" "C:\dev\Strata\serve\server.py" "--engine" "strata" "--config" "C:\dev\Strata\strata-swift-iq3_s.json" "--port" "1313" "--open"

if errorlevel 1 pause

