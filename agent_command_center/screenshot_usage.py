import sys
import os
import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        print("Launching headless Google Chrome...")
        browser = await p.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        # Set a standard desktop screen size
        await page.set_viewport_size({"width": 1280, "height": 800})
        
        # Navigate to login
        print("Navigating to login page...")
        page.on("console", lambda msg: print(f"CONSOLE: {msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))
        await page.goto("http://localhost:8000/accounts/login/")
        
        # Fill in username and password
        print("Logging in...")
        await page.fill('input[name="username"]', 'admin')
        await page.fill('input[name="password"]', 'antigravity-secure-2026')
        
        # Press login button
        await page.click('button[type="submit"]')
        
        # Wait for page navigation and ensure dashboard page loaded
        await page.wait_for_url("http://localhost:8000/")
        print("Login successful, reached dashboard!")
        
        # Wait a moment for page layout
        await page.wait_for_timeout(2000)
        
        # Click on 'Usage' agent
        print("Clicking on 'Usage' agent in sidebar...")
        await page.click('text=Usage')
        
        # Wait a few seconds for WebSocket connection and usage update
        await page.wait_for_timeout(3000)
        
        # Capture screenshot before modal is opened
        await page.screenshot(path="/root/agent_command_center/dashboard_loaded.png")
        print("Dashboard screenshot saved to /root/agent_command_center/dashboard_loaded.png")

        # Let's open the Model Quota & Usage modal
        print("Evaluating openUsageModal() in page context...")
        try:
            res = await page.evaluate("openUsageModal()")
            print("Evaluation successful, returned:", res)
            modal_info = await page.evaluate("""() => {
                const modal = document.getElementById('usage-modal');
                if (!modal) return 'No #usage-modal found!';
                const computed = window.getComputedStyle(modal);
                const cards = Array.from(document.querySelectorAll('#sidebar-usage-list > div')).map(card => {
                    const name = card.querySelector('span.font-bold').innerText;
                    const pctBar = card.querySelector('.bg-gradient-to-r').style.width;
                    const subSpan = card.querySelector('div.flex.justify-between span');
                    const subText = subSpan ? subSpan.innerText : '';
                    return { name, pctBar, subText };
                });
                return {
                    id: modal.id,
                    display: modal.style.display,
                    computedDisplay: computed.display,
                    rect: modal.getBoundingClientRect(),
                    cost: document.getElementById('usage-estimated-cost') ? document.getElementById('usage-estimated-cost').innerText : 'No cost',
                    cards: cards
                };
            }""")
            print("Modal element info & real data:", modal_info)
        except Exception as e:
            print("Evaluation failed with error:", e)
        
        # Wait a bit for the modal animation
        await page.wait_for_timeout(2000)
        
        # Take a screenshot showing the modal!
        await page.screenshot(path="/root/agent_command_center/modal_opened.png")
        print("Modal screenshot saved to /root/agent_command_center/modal_opened.png")
        
        await browser.close()

if __name__ == '__main__':
    asyncio.run(run())
