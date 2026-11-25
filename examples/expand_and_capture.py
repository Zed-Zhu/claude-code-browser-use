"""Expand every "show more" control, keep scrolling, and capture the page's own API responses.

Usage:
  python3 cbu.py run examples/expand_and_capture.py <url> <api-regex> [expand-selector] [scroll-selector] [out.json] [max-rounds]

Example (a comment thread whose replies load from /api/.../comment/):
  python3 cbu.py run examples/expand_and_capture.py "https://example.com/post/123" "comment/(page|sub)" ".show-more" ".comments" out.json

Runs inside `cbu.py run`, so `b` (Browser), `args`, `json`, `time` are predefined.
"""
url, pattern = args[0], args[1]
expand_sel = args[2] if len(args) > 2 else ""
scroll_sel = args[3] if len(args) > 3 else ""
out = args[4] if len(args) > 4 else "captured.json"
max_rounds = int(args[5]) if len(args) > 5 else 200

tab = b.open("about:blank")
b.watch(pattern, tab)          # watch BEFORE the page fires its requests
b.goto(url, tab)
time.sleep(3)

CLICK_ALL = """(sel) => { if (!sel) return 0; let n = 0;
  document.querySelectorAll(sel).forEach(e => { if (e.offsetParent) { e.click(); n++; } }); return n; }"""

idle = 0
for i in range(max_rounds):
    before = len(b.captured(tab)["captures"])
    clicked = b.call_js(CLICK_ALL, expand_sel, tab=tab)
    b.scroll(selector=scroll_sel or None, bottom=True, tab=tab)
    time.sleep(2.0)            # be gentle; this is the user's real account
    after = len(b.captured(tab)["captures"])
    print(f"round {i}: clicked {clicked}, responses {after}", flush=True)
    idle = idle + 1 if after == before else 0   # stop after 4 quiet rounds
    if idle >= 4:
        break

data = b.captured(tab)
open(out, "w").write(json.dumps(data["captures"], ensure_ascii=False))
print(json.dumps({"saved": out, "responses": len(data["captures"])}))
b.close(tab)
