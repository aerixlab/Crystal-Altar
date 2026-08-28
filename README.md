## We just release v1.0 EXE file. 
**Download**: Go to the Releases section to download the latest Windows version.

# Crystal Altar

A smooth, gallery-style image viewer for browsing photos, SVGs, and Silhouette Studio cut files — built to feel like a photo gallery, not a file manager.

> Personal project, built for my own workflow — not for sale or commercial distribution.

![Python](https://img.shields.io/badge/Python-3.9+-blue)
![PySide6](https://img.shields.io/badge/UI-PySide6-41cd52)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

## Features

- **Gallery-first design** — a clean thumbnail grid instead of a cluttered file-manager UI
- **Recursive folder scanning** — pick a root folder and every image inside it, including subfolders, shows up in one flat gallery
- **Multi-format support** — `.jpg` `.jpeg` `.png` `.svg`, plus Silhouette Studio cut files (`.studio` / `.studio3`)
- **Full-screen lightbox** with smooth crossfade transitions between images
- **Zoom & pan** — scroll to zoom, drag to pan, with crisp rendering at any zoom level (no blur when zoomed in)
- **Click-to-navigate** — click the blank margin beside the image (not just a tiny arrow) to go next/previous
- **Filmstrip** for quickly scrubbing through the current folder while in the lightbox
- **Sort** by name, date modified, size, or file type
- **Right-click menu** — open the real file in its default app, rename, delete (recycle bin support via `send2trash`), show in folder, copy path
- **Custom app icon** and a dark, minimalist UI

<img width="1333" height="916" alt="Screenshot 2026-08-28 142623" src="https://github.com/user-attachments/assets/0c2ddca6-f41c-401f-b449-c1cccfa1d83a" />

<img width="1323" height="909" alt="Screenshot 2026-08-28 144131" src="https://github.com/user-attachments/assets/8fa78255-0725-4445-a10d-f23467faa065" />

<img width="1322" height="906" alt="Screenshot 2026-08-28 142644" src="https://github.com/user-attachments/assets/6c679535-815a-4efe-ba7a-151892753bb5" />

<img width="1329" height="908" alt="Screenshot 2026-08-28 142706" src="https://github.com/user-attachments/assets/9ec1455f-8a8c-47e1-b27b-e57314d4d222" />



### Silhouette Studio (`.studio3`) previews

Silhouette Studio's file format is proprietary and undocumented, so there's no public library that fully parses it. Crystal Altar uses a best-effort heuristic: it scans the raw file for embedded preview images and renders the best one it finds with crisp (non-blurred) scaling, since these designs are dense fields of small dots that blur/merge together under smooth scaling. This gets close to — but won't perfectly match — the live vector rendering Silhouette Studio's own Windows shell extension produces for Explorer thumbnails.

Crystal Altar is an independent personal project and is not affiliated with, endorsed by, or sponsored by Silhouette America. "Silhouette Studio" is a product of Silhouette America; it's referenced here only to describe file compatibility.

## Download (.EXE): 
Go to the Releases section to download the latest Windows version.

## Installation

```bash
pip install PySide6 send2trash
python gallery_viewer.py
```

`send2trash` is optional — without it, deleting files is permanent instead of going to the recycle bin.

## Building a standalone executable (Windows)

```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --icon=gallery_icon.ico --name "Crystal Altar" gallery_viewer.py
```

The finished `.exe` will be in the `dist` folder. It's fully self-contained — copy just that one file anywhere to run it, no Python required on the target machine.

## Tech stack

- [Python 3](https://www.python.org/)
- [PySide6](https://doc.qt.io/qtforpython/) (Qt for Python) — UI, rendering, threading
- [send2trash](https://pypi.org/project/Send2Trash/) — optional recycle-bin support

## License

Personal use only. Not licensed for sale, resale, or commercial distribution.
