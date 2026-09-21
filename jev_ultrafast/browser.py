"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:

    def __init__(self, url=None, *, target_id=None):
        ensure_daemon()

        self.owned_target = target_id is None

        if target_id is not None:
            # Attach to an existing user tab.
            self.target = target_id

            # Make sure the tab we're controlling is visible.
            cdp("Target.activateTarget", targetId=self.target)

            self.session = cdp(
                "Target.attachToTarget",
                targetId=self.target,
                flatten=True,
            )["sessionId"]

        else:
            # Original demo behavior.
            self.target = cdp(
                "Target.createTarget",
                url="about:blank",
                background=True,
            )["targetId"]

            self.session = cdp(
                "Target.attachToTarget",
                targetId=self.target,
                flatten=True,
            )["sessionId"]

            self.call(
                "Emulation.setDeviceMetricsOverride",
                width=1120,
                height=780,
                deviceScaleFactor=1,
                mobile=False,
            )

            self.call(
                "Emulation.setFocusEmulationEnabled",
                enabled=True,
            )

            if url:
                self.call("Page.navigate", url=url)

        deadline = time.monotonic() + 15

        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except Exception:
                pass

            time.sleep(0.02)
    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const cache=window.__jevFast;
                      const frameId=action.frame_id || 'f0';
                      const field=cache?.resolve(frameId,action.node);
                      const doc=cache?.documentFor(frameId);
                      const win=doc?.defaultView;
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>doc?.getElementById(id)).filter(Boolean) : [doc];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<win.innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else win.requestAnimationFrame(ready);
                      };
                      if (!field || !doc || !win) finish();
                      else win.requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            frame_id = action.get("frame_id", "f0")
            guard_key = f"{frame_id}:{node}"
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard({json.dumps(frame_id)},"
                f"c.resolve({json.dumps(frame_id)},{node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(guard_key)]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if getattr(self, "session", None):
            try:
                cdp(
                    "Target.detachFromTarget",
                    sessionId=self.session,
                )
            except Exception:
                pass

            self.session = None

        if self.target and self.owned_target:
            try:
                cdp(
                    "Target.closeTarget",
                    targetId=self.target,
                )
            except Exception:
                pass

        self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned frame/node IDs refer to observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const cache=window.__jevFast;
              const frameId=action.frame_id || 'f0';
              const e=cache?.resolve(frameId,action.node);
              const doc=cache?.documentFor(frameId);
              const win=doc?.defaultView;
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!doc || !win || !r.width || !r.height || x<0 || y<0 ||
                  x>=win.innerWidth || y>=win.innerHeight) return null;
              if (!e.contains(cache.elementFromPoint(frameId,x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new win.Event('input',{bubbles:true}));
                e.dispatchEvent(new win.Event('change',{bubbles:true}));
              }
              const topRect=cache.topRect(frameId,e);
              if (!topRect) return null;
              const topX=topRect.x+topRect.w/2, topY=topRect.y+topRect.h/2;
              if (topX<0 || topY<0 || topX>=innerWidth || topY>=innerHeight) return null;
              return {x:topX,y:topY};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
