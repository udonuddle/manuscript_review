"""Exercise the standalone file in a real browser; no server required."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto((ROOT / "annotation.html").as_uri())
    assert page.locator(".block").count() == 10
    page.locator(".block").first.focus()
    page.keyboard.press("Enter")
    page.locator("#instruction").fill("Clarify <script>unsafe</script> aggregation.")
    page.locator("#reason").fill("Avoid overstating the measured result.")
    page.locator("#priority").select_option("high")
    page.locator("#type").select_option("claim")
    page.get_by_role("button", name="Save comment", exact=True).click()
    assert page.locator(".block.commented").count() == 1
    assert page.locator(".card").count() == 1
    page.reload()
    assert page.locator(".card").count() == 1
    page.get_by_role("button", name="Edit", exact=True).click()
    page.locator("#status").select_option("deferred")
    page.get_by_role("button", name="Save comment", exact=True).click()
    with page.expect_download() as download:
        page.get_by_role("button", name="Export comments.jsonl").click()
    assert download.value.suggested_filename == "comments.jsonl"
    records = [json.loads(line) for line in Path(download.value.path()).read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["status"] == "deferred"
    assert records[0]["priority"] == "high"
    assert records[0]["type"] == "claim"
    assert records[0]["paragraph_id"] == page.locator(".block").first.get_attribute("id")
    assert "<script>unsafe</script>" in records[0]["instruction"]
    page.screenshot(path="/tmp/annotation-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path="/tmp/annotation-mobile.png", full_page=True)
    page.on("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Delete", exact=True).click()
    assert page.locator(".card").count() == 0
    assert page.locator(".block.commented").count() == 0
    page.reload()
    assert page.locator(".card").count() == 0
    # Corrupt saved data is surfaced and never silently overwritten.
    page.evaluate("localStorage.setItem('manuscript-annotation-v1:manuscript/draft.md', 'broken')")
    page.reload()
    assert page.locator("#notice.error").count() == 1
    assert page.evaluate("localStorage.getItem('manuscript-annotation-v1:manuscript/draft.md')") == "broken"
    assert not errors, errors
    browser.close()
print("PASS: keyboard selection, add, persistence, edit, markers, JSONL download, delete, mobile layout, corrupt storage, no JS errors")
