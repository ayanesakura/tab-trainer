import time, pytest

pytestmark = pytest.mark.e2e


def test_dbg(server, browser):
    browser._run(browser._send("Page.navigate", url=f"{server['base']}/trainer?id={server['image']}"))
    time.sleep(6)
    try:
        print("READY", browser.js("document.readyState"), browser.js("typeof state"))
    except Exception as e:
        print("JSERR", e)
    print("ERRORS", browser.errors)
