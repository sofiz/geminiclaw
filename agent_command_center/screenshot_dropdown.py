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
        await page.set_viewport_size({"width": 1280, "height": 800})
        
        # Navigate to login
        print("Navigating to login page...")
        await page.goto("http://localhost:8000/accounts/login/")
        
        # Fill credentials
        print("Logging in...")
        await page.fill('input[name="username"]', 'admin')
        await page.fill('input[name="password"]', 'antigravity-secure-2026')
        await page.click('button[type="submit"]')
        
        # Wait for dashboard load
        await page.wait_for_url("http://localhost:8000/")
        print("Login successful, reached dashboard!")
        await page.wait_for_timeout(2000)
        
        # Select 'Usage' agent
        print("Clicking on 'Usage' agent...")
        await page.click('text=Usage')
        await page.wait_for_timeout(3000)
        
        # Click on settings/manage gear button
        print("Opening the management dropdown menu...")
        await page.click('#btn-manage-gear')
        await page.wait_for_timeout(1000)
        
        # Take screenshot of the opened dropdown
        await page.screenshot(path="/root/agent_command_center/dropdown_opened.png")
        print("Screenshot saved to /root/agent_command_center/dropdown_opened.png")
        
        await browser.close()

if __name__ == '__main__':
    asyncio.run(run())
