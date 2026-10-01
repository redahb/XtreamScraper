# Xtream-to-STRM

Xtream-to-STRM is a small standalone application that turns the **VOD** (Movies and Series) from one or more Xtream-compatible IPTV providers into a local library of `.strm` files. It can also scrape information and write it to `.nfo` files, compatible with **Jellyfin**, **Kodi**, **Emby**, or **Plex**.

**Live TV** is not supported, use [Dispatcharr](https://github.com/dispatcharr/dispatcharr) for that.

> [!NOTE]
> This was never intended to be a full-fledged product. I originally built it for personal use because the existing tools simply didn't do what I needed. I'm sharing it in case it's useful to others. I might update and support it, and I might look at pull requests. No guarantees.

## Requirements

- **Python 3.10** or newer
- One or more **IPTV providers**` that support **Xtream** codes
- **FFprobe** if you want to use the media probing functionality

> [!IMPORTANT]
> Xtream-to-STRM is intended for (and developed on) **Windows**, although it might also run on Linux and macOS.

## Installation

Clone or download the repository, then open a terminal in the project folder.

1. Create a virtual environment (this step is optional, but recommended):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install the dependencies:

```powershell
pip install -r requirements.txt
```

3. Start the application:

```powershell
python app.py
```

The WebUI can be found on [http://localhost:6060](http://localhost:6060) and provides configuration of providers, categories, metadata scrapers, artwork, probing and other settings.

## Output Library

The generated library **always** uses the following structure:

```text
<target>\<provider>\Movies\<category>\<Movie (Year)>\<Movie (Year)>.strm

<target>\<provider>\Series\<category>\<Series (Year)>\Season 01\<Series> S01E01.strm
```

For example:

```text
D:\XtreamLibrary\My Provider\Movies\4K\The Matrix (1999)\The Matrix (1999).strm

D:\XtreamLibrary\My Provider\Series\NETFLIX\Breaking Bad (2008)\Season 01\Breaking Bad S01E01.strm
```

Point your media server at the generated `Movies` and/or `Series` folders and let it scan them like a normal media library.

> [!WARNING]
> The code in this repository is heavily checked, improved, and at points even created by multiple AI coding agents. Claude Code is the master agent, but both Codex and Gemini Code Assist were involved by checking and optimizing.
>
>If you take issue with this, don't use this software.