# Creator Studio

One localhost app with two tabs:

- **Clip downloader**: paste a YouTube link and pick start/end, or let Gemini find the best or funniest moments, then download the clip. Every clip is also saved to `library/`.
- **ClipForge**: pick a clip from **Downloaded clips** (or press **Edit in ClipForge** right after a download), or drag in any video. Type instructions and get the edited short.

Open it with the **Creator Studio** shortcut (on the Desktop, in the Start menu, or in this folder). The shortcut starts the server in the background, with no console window, and opens the app in its own window. To pin it, right-click the shortcut and choose **Pin to taskbar**.

The server keeps running after you close the window. To shut it down, run **`Stop Creator Studio.pyw`**. `Start Creator Studio.cmd` still works too: it runs the server in a console window, which is useful for seeing errors. Background logs go to `studio.log`.

## Desktop app (Electron)

Download **Creator-Studio-Setup.exe** from the [Releases](https://github.com/gendesu4work/clipping-studio/releases) page and run it. It adds Desktop and Start menu shortcuts.

- **First launch** sets everything up in `%LOCALAPPDATA%\Creator Studio\app`: it creates a Python environment and installs the packages, which takes a few minutes. You need Python 3.10+ and `ffmpeg` on PATH.
- **Every launch** checks GitHub for updates, starts the server in the background and opens the app window.
- **Closing the window** stops the server.
- **Your data** (keys, library, outputs) lives in that folder and is kept when you reinstall or upgrade.

From a clone of this repo: `npm install`, then `npm start` to run the app, `npm run shortcut` to make shortcuts that open it, or `npm run dist` to build the installer into `dist/`.

## ClipForge

Drag in an interview or talking-head video, type what you want, and get back a vertical short in the same style as the reference video. You get:

- **No dead air.** It cuts every pause longer than 0.35s (adjustable with Pace), plus filler words, stutters and, with Claude, tangents.
- **Smooth head following.** The crop holds still while the speaker stays near the middle and glides when they move away. It snaps instantly on camera switches.
- **Word-by-word captions.** Bold uppercase with wide letter spacing. Each word fades in from grey as it's spoken, and keywords are colored with a soft glow.
- **Vignette.** A dark gradient at the bottom and a light shade at the top.
- **Music bed (optional).** The track loops and ducks automatically under the voice, and the mix is normalized to -14 LUFS.

## Run

Requirements: Python 3.10+ (`pip install -r requirements.txt`) and `ffmpeg` on PATH.

Your keys and sign-ins stay on your PC and are git-ignored: the Gemini key (`.env`), YouTube cookies (`cookies.txt`), the Claude key (`config.json`) and `settings.json`. Enter them in the app.

### Split layout (gaming clips)

For 16:9 gameplay with a facecam in a corner, choose **Layout → Split · reaction cam + gameplay**. You can also type "gaming", "split" or "facecam" in the instructions.

- **Top:** the reaction cam, with the same smooth face-follow.
- **Bottom:** the gameplay.
- **Captions** sit on the line between the two panels.
- **Everything else is unchanged:** dead-air cuts, captions, music and loudness work the same as in Full frame.

**Auto-detection.** It finds the face that stays in one place across the video (faces in the gameplay move around), then the facecam overlay's border. The gameplay crop slides sideways so the facecam doesn't show up inside it.

**Box editor.** Drag the yellow (cam) and blue (gameplay) boxes on a frame of your video. Drag a box's corner dot to resize it. **Cam height** sets how much of the 9:16 frame the reaction cam takes. **Frame** picks which moment of the video to look at. The preview on the right shows the final layout.

Command line: `python pipeline.py in.mp4 out.mp4 --layout split`

## Instructions

- **Without an API key**, built-in rules handle:
  - "under 30 seconds"
  - "9:16" / "square"
  - "no captions"
  - "captions at the top"
  - "no music"
  - "snappy" / "keep the pauses"
- **With an Anthropic API key** (the **Claude** button, top right), Claude reads the transcript. It picks the best story, cuts tangents, puts the hook first and colors keywords by meaning.

## Files

- `pipeline.py`: the editing engine. Also usable from the command line: `python pipeline.py in.mp4 out.mp4 --instructions "..." --music song.mp3`.
- `director.py`: turns your instructions into an edit plan (Claude or built-in rules).
- `server.py`: the combined server. `static/index.html` is the two-tab shell.
- `clipper.py` + `static/clipper.html`: the YouTube clip downloader.
- `forge_routes.py` + `static/forge.html`: ClipForge and the shared clip library.
- `library/`: every downloaded clip.
- `jobs/`: uploads and finished edits.
- `models/`: the YuNet face detector.
