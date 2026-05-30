#!/usr/bin/env python3
import sys
import os
import argparse
from playwright.sync_api import sync_playwright

def main():
    parser = argparse.ArgumentParser(description="Run headless Google Chrome automation.")
    parser.add_argument("--url", required=True, help="The URL to navigate to.")
    parser.add_argument("--screenshot", help="File path to save the page screenshot.")
    parser.add_argument("--extract-text", action="store_true", help="Extract and print all text from the page body.")
    parser.add_argument("--extract-html", action="store_true", help="Extract and print page source HTML.")
    parser.add_argument("--click", help="CSS selector of element to click.")
    parser.add_argument("--type-selector", help="CSS selector of input element to type into.")
    parser.add_argument("--type-text", help="Text to type into type-selector.")
    parser.add_argument("--wait-selector", help="CSS selector to wait for before performing actions.")
    parser.add_argument("--timeout", type=int, default=30000, help="Navigation and action timeout in milliseconds.")
    
    args = parser.parse_args()
    
    chrome_path = "/usr/bin/google-chrome"
    if not os.path.exists(chrome_path):
        print(f"Error: Google Chrome executable not found at {chrome_path}", file=sys.stderr)
        sys.exit(1)
        
    with sync_playwright() as p:
        print(f"Launching headless Chrome (no-sandbox) to visit {args.url}...", file=sys.stderr)
        browser = p.chromium.launch(
            executable_path=chrome_path,
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        try:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = context.new_page()
            
            # Navigate to the URL
            response = page.goto(args.url, timeout=args.timeout)
            page.wait_for_load_state("domcontentloaded")
            
            print(f"Loaded page title: {page.title()}", file=sys.stderr)
            
            if args.wait_selector:
                print(f"Waiting for selector: {args.wait_selector}...", file=sys.stderr)
                page.wait_for_selector(args.wait_selector, timeout=args.timeout)
                
            if args.type_selector and args.type_text:
                print(f"Typing into {args.type_selector}...", file=sys.stderr)
                page.fill(args.type_selector, args.type_text)
                
            if args.click:
                print(f"Clicking on {args.click}...", file=sys.stderr)
                page.click(args.click)
                # Wait for navigation/load if click caused action
                page.wait_for_timeout(2000)
                
            if args.screenshot:
                print(f"Saving screenshot to {args.screenshot}...", file=sys.stderr)
                page.screenshot(path=args.screenshot, full_page=True)
                print(f"Screenshot saved successfully to {args.screenshot}", file=sys.stderr)
                
            if args.extract_text:
                text = page.locator("body").inner_text()
                print("\n=== EXTRACTED BODY TEXT ===")
                print(text)
                print("===========================")
                
            if args.extract_html:
                html = page.content()
                print("\n=== EXTRACTED HTML SOURCE ===")
                print(html)
                print("=============================")
                
        except Exception as e:
            print(f"Error during execution: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            browser.close()

if __name__ == "__main__":
    main()
