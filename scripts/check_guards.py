"""Local-browser freshness/execution regressions. No model calls or external websites."""

from html import escape
from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<p id="outside">Unrelated offscreen text</p>"""

FRAME_CONTENT = """<!doctype html><style>
body{margin:20px}.hidden-control{position:absolute;opacity:0;width:1px;height:1px}
label,input,select{display:block;margin:12px}
</style><h1>IPv6 question</h1>
<label><input class="hidden-control" id="answer" type="checkbox">Neighbor advertisement</label>
<div role="checkbox" aria-checked="false" aria-labelledby="composite-label">
  <input class="hidden-control" id="composite" type="checkbox">
  <label id="composite-label" for="composite">Composite choice</label>
</div>
<label><input type="radio" name="message">Router solicitation</label>
<input aria-label="Course answer">
<select aria-label="Category"><option>All</option><option>Neighbor</option></select>
<div id="shadow-host"></div><script>
const shadow=document.querySelector('#shadow-host').attachShadow({mode:'open'});
shadow.innerHTML='<button id="shadow-action">Shadow action</button>';
shadow.querySelector('button').onclick=()=>window.shadowClicks=(window.shadowClicks||0)+1;
</script>"""
FRAME_PAGE = f"""<!doctype html><style>
body{{margin:20px}}iframe{{display:block;width:600px;height:300px;margin:30px 0 0 70px;border:4px solid}}
#hidden{{display:none}}
</style><button>Outer button</button>
<iframe title="Course content" srcdoc="{escape(FRAME_CONTENT, quote=True)}"></iframe>
<iframe id="hidden" srcdoc="<button>Hidden auth action</button>"></iframe>"""


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        browser.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
        assert browser.fresh(page), "Movement should use fresh geometry, not another model call"
        browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        passed.append("moving target clicked at its current location")

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        # A textless overlay does not alter the model's semantic state, but must block a click.
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert browser.fresh(page)
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + repr("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio">Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <select id="category" aria-label="Category">
            <option>All</option><option>Design</option><option disabled>Unavailable</option>
          </select></form><aside id="unrelated">News</aside>
        """))
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]
        passed.append("native controls expose only supported operations and safe values")

        select = next(a for a in actions if a["kind"] == "select")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        passed.append("native dropdown selects an observed option")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.act(field, page, text="Generated")
        page = browser.observe(screenshot=False)
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")

        browser.call("Page.navigate", url="data:text/html," + quote(FRAME_PAGE))
        for _ in range(100):
            if browser.evaluate("document.querySelector('iframe')?.contentDocument?.readyState") == "complete":
                break
        page = browser.observe(screenshot=False)
        labels = [action["label"] for action in page["actions"]]
        assert labels.count("Neighbor advertisement") == 1
        assert labels.count("Composite choice") == 1
        assert labels.count("Router solicitation") == 1
        assert "Course answer" in labels and "Shadow action" in labels
        assert "Hidden auth action" not in labels
        inner = next(a for a in page["actions"] if a["label"] == "Neighbor advertisement")
        outer = next(a for a in page["actions"] if a["label"] == "Outer button")
        assert inner["frame_id"] != outer["frame_id"]
        assert inner["rect"]["x"] > 70 and inner["rect"]["y"] > 30
        assert "IPv6 question" in page["text"]
        passed.append("visible same-origin frame merges controls, text, and translated geometry once")

        browser.act(inner, page)
        checked = browser.evaluate("document.querySelector('iframe').contentDocument.querySelector('#answer').checked")
        assert checked is True
        passed.append("label-backed iframe checkbox clicks through top-page coordinates")

        page = browser.observe(screenshot=False)
        shadow_action = next(a for a in page["actions"] if a["label"] == "Shadow action")
        browser.act(shadow_action, page)
        assert browser.evaluate("document.querySelector('iframe').contentWindow.shadowClicks") == 1
        passed.append("iframe shadow-root control passes deep hit testing and clicks")

        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill" and a["label"] == "Course answer")
        browser.act(field, page, text="neighbor")
        browser.observe(screenshot=False)
        value = browser.evaluate(
            "document.querySelector('iframe').contentDocument"
            ".querySelector('[aria-label=\"Course answer\"]').value"
        )
        assert value == "neighbor"
        passed.append("iframe text entry uses the frame-local node cache")

        page = browser.observe(screenshot=False)
        select = next(a for a in page["actions"] if a["kind"] == "select" and a["value"] == "Neighbor")
        browser.act(select, page)
        value = browser.evaluate("document.querySelector('iframe').contentDocument.querySelector('select').value")
        assert value == "Neighbor"
        passed.append("iframe dropdown uses the frame-local node cache")

        page = browser.observe(screenshot=False)
        radio = next(a for a in page["actions"] if a["label"] == "Router solicitation")
        browser.evaluate("document.querySelector('iframe').contentDocument.querySelector('input[type=radio]').checked=true")
        assert not browser.fresh(page, radio)
        passed.append("iframe semantic changes invalidate frame-aware freshness guards")

        browser.call("Page.navigate", url="about:blank")
        assert not browser.fresh(page, field)
        passed.append("navigation invalidates the old document")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
