import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        await page.goto("http://localhost:8000/accounts/login/")
        await page.fill('input[name="username"]', 'admin')
        await page.fill('input[name="password"]', 'antigravity-secure-2026')
        await page.click('button[type="submit"]')
        await page.wait_for_url("http://localhost:8000/")
        await page.wait_for_timeout(2000)
        await page.click('text=Usage')
        await page.wait_for_timeout(2000)
        await page.click('#btn-manage-gear')
        await page.wait_for_timeout(1000)

        # Get computed styles
        styles = await page.evaluate("""() => {
            const getInfo = (selector) => {
                const el = document.querySelector(selector);
                if (!el) return selector + " not found!";
                const comp = window.getComputedStyle(el);
                return {
                    selector: selector,
                    className: el.className,
                    position: comp.position,
                    zIndex: comp.zIndex,
                    display: comp.display,
                    opacity: comp.opacity,
                    filter: comp.filter,
                    transform: comp.transform
                };
            };
            return {
                header: getInfo('header'),
                manageBtnParent: getInfo('#btn-manage-gear') ? getInfo('#btn-manage-gear').selector : 'none',
                dropdown: getInfo('#manage-dropdown-menu'),
                main: getInfo('main'),
                centerPanel: getInfo('section.flex-1.flex.flex-col'),
                chatTimeline: getInfo('#chat-timeline'),
                chatBubble: getInfo('.animate-chat-bubble')
            };
        }""")
        import pprint
        pprint.pprint(styles)
        await browser.close()

if __name__ == '__main__':
    asyncio.run(run())
