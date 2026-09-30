@echo off
rem Rebuilds app.js from src\main.js (bundles three.js so the site also works from file://)
rem Requires Node.js. Uses esbuild via npx.
cd /d "%~dp0"
call npx --yes esbuild src/main.js --bundle --minify --format=iife --target=es2019 --outfile=app.js
