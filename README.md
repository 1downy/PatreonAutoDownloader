# Patreon Auto Downloader

A simple tool that automatically downloads files from Patreon posts.

## What it does
When you copy a Patreon link, this tool automatically finds the files in that post and downloads them for you. It stays open in the background and watches for new links.

## How to use it
1. **Setup**: Run these two commands to get things ready:
   ```powershell
   pip install -r requirements.txt
   playwright install chromium
   ```
2. **Start**: Open a terminal and run the script:
   ```powershell
   python main.py
   ```
3. **Download**: Just copy a Patreon post link (like `https://www.patreon.com/posts/...`) and the tool will start downloading.

---
*Note: Use this responsibly and support creators!*
