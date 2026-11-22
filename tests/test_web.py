"""Guards for the web page in app/web. They read the three files and need no browser.

Two of them run a function of app.js in node, and are skipped where node is not installed.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "app" / "web"
FILES = ("index.html", "app.js", "style.css")

# The data-testid hooks the page promises. A trailing hyphen marks a prefix that an item id completes.
HOOKS = (
    "order-card-", "context-bar", "messages", "message-customer", "message-agent", "chat-input", "send",
    "quick-track", "quick-refund", "quick-cancel", "quick-return", "quick-missing",
    "confirm-action", "ticket-ref", "item-choice-", "retry", "dev-toggle", "trace-card",
    "change-item", "item-picker", "thread-divider", "pending-status", "ticket-copy",
    "trace-open", "trace-drawer", "trace-close", "more-help",
    "thread-start", "general-question", "skip",
)

# Every way of handing text to the HTML parser or to the script engine.
SINKS = (
    "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "setHTMLUnsafe",
    "createContextualFragment", "DOMParser", "srcdoc", "javascript:", "eval(", "Function(",
)

# Attribute names that run script, navigate or style when text reaches them.
EXECUTING = r"(?:on\w+|href|srcdoc|style|action|formaction)"


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def function(name: str) -> str:
    """The source of a top-level function of app.js, from its first line to its closing brace."""
    found = re.search(rf"^(?:async )?function {name}\(.*?^}}\n", read("app.js"), flags=re.DOTALL | re.MULTILINE)
    assert found, f"app.js has no top-level function {name}"
    return found.group()


def run_in_node(source: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr


# ---- nothing is parsed, nothing executes, nothing leaves the page ----


@pytest.mark.parametrize("name", ["app.js", "index.html"])
@pytest.mark.parametrize("sink", SINKS)
def test_text_is_never_parsed_as_html_or_script(name, sink):
    """Server and user text reaches the page as text nodes only."""
    assert sink not in read(name)


def test_page_wires_nothing_in_its_markup():
    """index.html loads one script file. It has no inline script and no event-handler attribute."""
    page = read("index.html")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page, flags=re.DOTALL | re.IGNORECASE)

    assert [body.strip() for _, body in scripts] == [""]
    assert re.search(r'\bsrc="app\.js\b', scripts[0][0])
    assert not re.search(r"\son\w+\s*=", page, flags=re.IGNORECASE)


def test_script_sets_no_attribute_that_executes():
    """el() copies its attribute names from object literals, so none of them, and no setAttribute call, may
    name a handler, a link or a style: those would run or navigate with whatever text they were given."""
    script = read("app.js")

    assert not re.search(rf"""setAttribute\(\s*["'`]{EXECUTING}["'`]""", script, flags=re.IGNORECASE)
    assert not re.search(rf"""[{{,]\s*["']?{EXECUTING}["']?\s*:""", script, flags=re.IGNORECASE)
    assert not re.search(rf"[{{,]\s*{EXECUTING}\s*[,}}]", script, flags=re.IGNORECASE)  # the shorthand { href }


@pytest.mark.parametrize("name", FILES)
def test_page_references_nothing_outside_itself(name):
    """The page runs in an iframe that must make no external request: no URL with a scheme, and no
    protocol-relative one in a string, a template, a url() or a bare attribute."""
    found = re.findall(r"[a-z][a-z0-9+.-]*://|[\"'`(=]//", read(name), flags=re.IGNORECASE)

    assert found == []


# ---- the hooks and the palette ----


@pytest.mark.parametrize("hook", HOOKS)
def test_hook_is_declared(hook):
    """Each hook is the value of a data-testid, in the page or in the script that builds the rest of it."""
    end = r"\$\{" if hook.endswith("-") else "[\"'`]"
    declared = re.compile(rf"data-testid\W[^\n]{{0,60}}?[\"'`]{re.escape(hook)}{end}")

    assert declared.search(read("index.html")) or declared.search(read("app.js"))


@pytest.mark.parametrize(
    "name, colour", [("ivory", "#FAF8F5"), ("ink", "#242124"), ("surface", "#FFFFFF"), ("plum", "#59354C")]
)
def test_stylesheet_defines_and_uses_the_brief_colours(name, colour):
    """Ivory page, near-black text, white surfaces and the one plum accent: defined once, and in use."""
    sheet = read("style.css")

    assert re.search(rf"--{name}:\s*{colour}\b", sheet, flags=re.IGNORECASE)
    assert f"var(--{name})" in sheet


# ---- the API contract and the session ----


def test_script_keeps_the_session_contract():
    """The server-issued token stays in sessionStorage under the same key and travels in the X-Session header
    of every request but the one that starts the session. URLs stay relative."""
    script = read("app.js")
    headers = re.findall(r"\{[^{}]*\}", re.search(r"headers:([^\n]*)", script).group(1))

    assert 'const SESSION_KEY = "jev-demo-session";' in script
    assert "sessionStorage.getItem(" in script
    assert "sessionStorage.setItem(" in script
    assert "localStorage" not in script
    assert script.count("fetch(") == 2  # the session start, and api() for everything else
    assert headers and all('"X-Session"' in each for each in headers)
    assert not re.search(r"""["'`]/""", script)  # no path from the site root: the page may be served under a prefix


@pytest.mark.parametrize(
    "piece",
    [
        "res.status === 401",  # the reset when the session is gone
        "await resetDemo();",
        "status === 429",  # the message limit
        "data.limit_reached",
        "payload.retry",
        "payload.item_choices",
        "payload.pending_action",
        "payload.ticket_ref",
        "payload.item_id && !sent.itemId && !selected",  # adopt the item the server resolved, and only then
    ],
)
def test_script_keeps_the_api_flows(piece):
    assert piece in read("app.js")


def test_pending_status_comes_up_after_half_a_second():
    script = read("app.js")

    assert "const PENDING_AFTER_MS = 500;" in script
    assert "}, PENDING_AFTER_MS);" in script
    assert re.search(r"""["']pending-status["'],\s*role:\s*["']status["']""", script)


# ---- one customer action is one request ----


def test_every_path_that_sends_asks_the_guard_first():
    """A message goes out only through send(), a confirmation only through confirmAction(), and both ask
    accept() before they do anything."""
    script = read("app.js")

    assert script.count('"api/chat"') == 1 and '"api/chat"' in function("send")
    assert script.count("api/actions/") == 1 and "api/actions/" in function("confirmAction")
    assert re.match(r"[^\n]*\n\s*if \(!accept\(", function("send"))
    assert re.match(r"[^\n]*\n\s*if \([^\n]*!accept\(", function("confirmAction"))


def test_the_same_action_is_not_accepted_twice_within_a_moment():
    """accept() in node, on a clock the test moves."""
    repeat = re.search(r"^const REPEAT_MS = \d+;", read("app.js"), flags=re.MULTILINE).group()
    run_in_node(
        f"""
        let busy = false, last = {{ key: "", at: 0 }}, clock = 5000;
        const performance = {{ now: () => clock }};
        {repeat}
        {function("accept")}
        const assert = require("node:assert/strict");
        assert.equal(accept("a"), true, "the first press");
        clock += 20;
        assert.equal(accept("a"), false, "the second click of a double click");
        clock += REPEAT_MS - 100;
        assert.equal(accept("a"), false, "a held key: each dropped repeat starts the moment again");
        clock += REPEAT_MS - 100;
        assert.equal(accept("a"), false, "still held");
        clock += 10;
        assert.equal(accept("b"), true, "another action is not held back");
        clock += REPEAT_MS + 1;
        assert.equal(accept("b"), true, "the same action is a new one after the moment has passed");
        busy = true;
        clock += 10 * REPEAT_MS;
        assert.equal(accept("c"), false, "nothing is accepted while a request is in flight");
        """
    )


def test_a_busy_control_is_marked_unavailable_and_never_disabled():
    """A disabled button drops the keyboard focus to the page, so while a request is in flight the controls
    that would send are marked with aria-disabled, and the script disables nothing."""
    script = read("app.js")

    assert 'control.setAttribute("aria-disabled", String(on));' in function("setBusy")
    assert not re.search(r"""\.disabled\b|["'`]disabled["'`]""", script)


# ---- the page is never wider than the screen ----


def test_the_page_cannot_grow_wider_than_the_screen():
    """The page is one grid column that may shrink below its content (the masthead needs 370px on one line),
    and below that width the developer toggle drops a word."""
    sheet = read("style.css")
    body = re.search(r"^body \{.*?^\}", sheet, flags=re.DOTALL | re.MULTILINE).group()
    small = re.search(r"@media \(max-width: ([\d.]+)rem\) \{\s*\.masthead \{.*?\.mode__word \{\s*display: none;", sheet, flags=re.DOTALL)

    assert "grid-template-columns: minmax(0, 1fr);" in body
    assert small and float(small.group(1)) * 16 >= 370


# ---- each reply opens its own trace ----


def test_traces_are_read_only_in_developer_mode():
    """GET /api/trace returns drafts that were never verified. The customer view has no use for them, so the
    one function that reads them returns at once unless developer mode is on."""
    script = read("app.js")

    assert script.count('"api/trace"') == 1 and '"api/trace"' in function("loadTraces")
    assert re.match(r'[^\n]*\n\s*if \(!document\.body\.classList\.contains\("dev"\)', function("loadTraces"))



def test_replies_are_paired_with_their_own_traces():
    """matchTraces() in node. Identical exchanges are told apart by their order alone."""
    run_in_node(
        f"""
        {function("matchTraces")}
        const assert = require("node:assert/strict");
        const trace = (message, reply) => ({{ message, reply }});
        const asked = [{{ message: "a", text: "x" }}, {{ message: "a", text: "x" }}, {{ message: "b", text: "y" }}];
        assert.deepEqual(matchTraces(asked, [trace("a", "x"), trace("a", "x"), trace("b", "y")]), [0, 1, 2]);
        // traces from before a reload belong to no reply on the page
        assert.deepEqual(matchTraces(asked, [trace("a", "x"), trace("a", "x"), trace("a", "x"), trace("b", "y")]), [1, 2, 3]);
        // the same words answered differently are different exchanges
        assert.deepEqual(matchTraces(asked, [trace("a", "x"), trace("a", "other"), trace("a", "x"), trace("b", "y")]), [0, 2, 3]);
        // a trace the server never stored leaves the oldest of the identical replies without one
        assert.deepEqual(matchTraces(asked, [trace("a", "x"), trace("b", "y")]), [-1, 0, 1]);
        assert.deepEqual(matchTraces(asked, []), [-1, -1, -1]);
        assert.deepEqual(matchTraces([], [trace("a", "x")]), []);
        """
    )


def test_script_parses():
    """A syntax error would stop the whole page, and nothing else here would notice."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run([node, "--check", str(WEB / "app.js")], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
