@echo off
cd /d "B:\Grocery list price comparison"
echo ===== startup %date% %time% ===== >> startup.log
start "" pythonw app.py
