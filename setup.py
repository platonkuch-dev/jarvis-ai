"""Install the Python runtime required by MARK XLVIII."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def run(*args: str) -> None:
	subprocess.run([sys.executable, *args], check=True, cwd=ROOT)


def main() -> None:
	print("Installing MARK XLVIII Python requirements...")
	run("-m", "pip", "install", "--upgrade", "pip")
	run("-m", "pip", "install", "-r", "requirements.txt")

	print("Installing Playwright browsers...")
	run("-m", "playwright", "install", "chromium")

	print("\nSetup complete. Start JARVIS with: python main.py")
	print("Optional Windows autostart: python main.py --install-autostart")


if __name__ == "__main__":
	main()

