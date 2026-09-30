@echo off
chcp 65001 >nul
rem Скачивает Qwen3-8B (~5 ГБ) и собирает из неё модель "jarvis-qwen3"
rem с большим контекстом. Нужна установленная Ollama (ollama.com).
cd /d "%~dp0"
where ollama >nul 2>nul || (echo Ollama не найдена. Установите её с ollama.com & pause & exit /b 1)
ollama pull qwen3:8b || (pause & exit /b 1)
ollama create jarvis-qwen3 -f ollama\Jarvis-qwen3.Modelfile || (pause & exit /b 1)
echo.
echo Готово. В .env поставьте:  LLM_PROVIDER=ollama   OLLAMA_MODEL=jarvis-qwen3
pause
