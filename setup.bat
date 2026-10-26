@echo off
cd /d "%~dp0"
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
python seed_admin.py
python app.py
