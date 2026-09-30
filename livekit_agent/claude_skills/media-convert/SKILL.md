---
name: media-convert
description: Конвертация и обработка видео, аудио и картинок через ffmpeg и Pillow — сжать видео, поменять формат (mp4, mkv, mov, avi, webm, mp3, wav, flac, jpg, png, webp, gif), вырезать кусок, извлечь звук, сделать гифку, склеить, повернуть, уменьшить размер картинок пачкой, узнать длительность/разрешение. Use for "сожми видео", "сделай mp3 из видео", "конвертируй", "обрежь видео", "сделай гифку", "уменьши фотки".
---

# Медиа: конвертация и обработка

`ffmpeg` установлен и доступен в PATH. Проверка: `ffmpeg -version`. Если его нет — поставь через
software-manager (`winget install --id Gyan.FFmpeg -e`, нужно подтверждение).

Правила:
- Исходник никогда не перезаписывай: результат клади рядом с суффиксом (`video_small.mp4`)
  или туда, куда сказал пользователь.
- Всегда добавляй `-hide_banner -loglevel error -y` и проверяй, что файл появился и не пустой.
- Долгая конвертация (видео больше пары минут) — сначала скажи «конвертирую, это займёт
  немного времени», запускай с `run_in_background`, если инструмент это позволяет.
- Итог — одной фразой: что получилось, размер до и после.

## Информация о файле

```powershell
ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height -of json "in.mp4"
```

## Видео

```powershell
# сжать (CRF 23 — норм. качество, 28 — сильнее сжатие); NVIDIA быстрее: -c:v h264_nvenc -cq 28
ffmpeg -hide_banner -loglevel error -y -i in.mp4 -c:v libx264 -crf 28 -preset medium -c:a aac -b:a 128k in_small.mp4
# под Telegram/Discord до ~25 МБ: уменьшить разрешение
ffmpeg -hide_banner -loglevel error -y -i in.mp4 -vf "scale=-2:720" -c:v libx264 -crf 28 -c:a aac -b:a 96k in_720p.mp4
# сменить контейнер без перекодирования
ffmpeg -hide_banner -loglevel error -y -i in.mkv -c copy in.mp4
# вырезать фрагмент (с 1:30 длиной 45 секунд)
ffmpeg -hide_banner -loglevel error -y -ss 00:01:30 -i in.mp4 -t 45 -c:v libx264 -crf 20 -c:a aac cut.mp4
# повернуть на 90° по часовой
ffmpeg -hide_banner -loglevel error -y -i in.mp4 -vf "transpose=1" -c:a copy rotated.mp4
# склеить (одинаковые кодеки): list.txt со строками  file 'a.mp4'
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i list.txt -c copy joined.mp4
# гифка (10 fps, ширина 480)
ffmpeg -hide_banner -loglevel error -y -ss 5 -t 4 -i in.mp4 -vf "fps=10,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" out.gif
# кадр-превью
ffmpeg -hide_banner -loglevel error -y -ss 10 -i in.mp4 -frames:v 1 frame.jpg
```

## Аудио

```powershell
ffmpeg -hide_banner -loglevel error -y -i video.mp4 -vn -c:a libmp3lame -q:a 2 audio.mp3   # звук из видео
ffmpeg -hide_banner -loglevel error -y -i in.wav -c:a libmp3lame -b:a 192k out.mp3
ffmpeg -hide_banner -loglevel error -y -i in.mp3 -af "loudnorm" normalized.mp3            # выровнять громкость
ffmpeg -hide_banner -loglevel error -y -i in.mp3 -ss 0 -t 30 -c copy first30s.mp3
```

## Картинки (пачкой)

```powershell
python -c "
from pathlib import Path; from PIL import Image
src = Path(r'C:\путь\к\папке'); out = src / 'small'; out.mkdir(exist_ok=True)
for p in src.glob('*'):
    if p.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.heic'):
        im = Image.open(p); im.thumbnail((1920,1920)); im.convert('RGB').save(out / (p.stem + '.jpg'), quality=85)
"
```
HEIC нужен `pillow-heif` (`python -m pip install --user pillow-heif` и `from pillow_heif import register_heif_opener; register_heif_opener()`).

Скачать видео с YouTube и других сайтов — скилл video-downloader.
