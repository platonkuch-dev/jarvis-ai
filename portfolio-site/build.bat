@echo off
rem Rebuilds app.js from src\main.js (bundles three.js so the site also works from file://)
rem and copies the site into deploy\ for Cloudflare Pages. Requires Node.js. Uses esbuild via npx.
cd /d "%~dp0"
call npx --yes esbuild src/main.js --bundle --minify --format=iife --target=es2019 --outfile=app.js || exit /b 1
for %%f in (index.html style.css app.js robots.txt admin.html _headers) do copy /y %%f deploy\ >nul
echo Built. Deploy with:  npx wrangler pages deploy
