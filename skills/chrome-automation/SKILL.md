---
name: chrome-automation
description: Activates when the user prompt contains "/chrome" or requests headless Chrome, background browser, web scraping, screenshots, or automated browsing. Spawns headless Chrome and performs user-requested actions.
version: "1.0.0"
---

# Chrome Headless Automation Skill

This skill allows the agent to run background headless Google Chrome instances to browse pages, scrape content, take screenshots, or fill out forms based on a user's instructions.

## Quick Trigger
Any prompt containing `/chrome` or requesting background/automated browser actions will activate this skill.

## Capabilities & Tooling
* **Chrome Executable**: `/usr/bin/google-chrome`
* **Python virtualenv**: `/root/chrome/venv` (includes `websocket-client` package for direct CDP control)
* **Pre-built CLI Helper**: `/root/.gemini/antigravity-cli/skills/chrome-automation/scripts/chrome_run.py`
  * Call using: `/root/chrome/venv/bin/python3 /root/.gemini/antigravity-cli/skills/chrome-automation/scripts/chrome_run.py`
  * Example: `/root/chrome/venv/bin/python3 /root/.gemini/antigravity-cli/skills/chrome-automation/scripts/chrome_run.py --url "https://news.ycombinator.com" --screenshot "/root/screenshot.png" --extract-text`

## Execution Instructions
When a user prompt begins with `/chrome` or requests browser automation:
1. **Analyze the Request**: Parse the URL, targets, selectors, text to type, or screenshot saving requirements.
2. **Select the Best Tool**:
   * For simple or standard tasks (navigate, screenshot, extract text, wait for element, click, type): Run the pre-built script `/root/.gemini/antigravity-cli/skills/chrome-automation/scripts/chrome_run.py` using `/root/chrome/venv/bin/python3` via `run_command`.
   * For highly customized, multi-step tasks: Write a custom, one-off python script in your current workspace (or `/tmp`) using standard WebSocket to connect to Chrome's remote debugging port (`9222`) and execute CDP commands directly, then execute it using `/root/chrome/venv/bin/python3`.
3. **Important Safety & Sandboxing Flags**: Always launch chromium with:
   * `--headless=new`
   * `--remote-debugging-port=9222`
   * `--no-sandbox`
   * `--disable-setuid-sandbox`
   * `--disable-dev-shm-usage`
   * `--disable-extensions`
   * `--remote-allow-origins=*`
   * Example Custom CDP Launch and Connect:
     ```python
     import subprocess, urllib.request, json
     from websocket import create_connection
     
     proc = subprocess.Popen([
         "/usr/bin/google-chrome", "--headless=new", "--remote-debugging-port=9222",
         "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
         "--disable-extensions", "--remote-allow-origins=*", "about:blank"
     ])
     # Connect to http://127.0.0.1:9222/json/list to get ws_url, then use create_connection
     ```
4. **Display Results**: Print extracted textual content, list results, or link/display screenshots back to the user clearly.
