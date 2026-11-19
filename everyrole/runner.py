"""Run one flow: agent + guard + secret-safe values + budgets + deterministic expectations.

Status is decided ONLY by `expect` checks on a fresh read of the page, never by the agent's DONE:
  passed | failed_expect | forbidden_action | left_allowed_hosts | budget_exceeded | blocked | error

Each flow runs in its own isolated browser context. A persona's saved session is restored before the
first page load; a passing login flow saves its session for the others.
`expect_denied` flows never stop early: the agent must really try, and only the checks decide.
Checks are judged on a SETTLED page: when they pass, the runner waits for the page to stop changing and
re-checks, so text that only exists while loading ("Opening…", skeletons) never passes a flow.
"""

import base64
import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from jev_ultrafast.agent import BudgetExceeded
from jev_ultrafast.model import SecretField

from . import account_book, assertions, heartbeat, inject, mailbox, usage, verdict, writeguard
from .guard import Guard, GuardViolation
from .model_check import ROLES as MODEL_ROLES
from .model_check import answered_slot, usage_group
from .pagewait import chain, track_network, wait_until_stable
from .redact import Redactor
from .session import capture, restorer, save
from .typed_values import TypedValues
from .values import ValueChooser

# Where a person clicks an element: the middle of one of its own boxes. A link that wraps onto two lines has two
# boxes, and the middle of the rectangle around both is often on neither (a footer link wrapped onto
# two lines: the middle hit its list item, read as "covered"). Each box is tried in turn; when none
# is the element under the pointer, what is on top (the fixed or sticky layer, or the element hit) covers it.
POINT_ON = r"""(el => {
  const view = r => r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 && r.left < innerWidth
    && r.top < innerHeight;
  const boxes = [...el.getClientRects()].filter(view);
  for (const r of boxes) {
    const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
    const y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
    const hit = document.elementFromPoint(x, y);
    if (hit && (hit === el || el.contains(hit))) return {x, y};
  }
  const r = boxes[0] || el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!hit) return {covered: true, by: 'nothing in view'};
  let top = hit;
  const pinned = e => ['fixed', 'sticky'].includes(getComputedStyle(e).position);
  while (top && top !== document.body && !pinned(top)) top = top.parentElement;
  const over = top && top !== document.body ? top : hit;
  const said = over.innerText || over.getAttribute('aria-label') || over.tagName.toLowerCase();
  return {covered: true, by: said.replace(/\s+/g, ' ').trim().slice(0, 80)};
})"""
# finds the link to a page (its path), the one whose words match when several lead there, scrolls it to the middle of
# the screen and says where to click, or what covers it there. Words compare as the snapshot keeps them (digits as #).
CLICK_LINK = r"""((to, query, label) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().replace(/[0-9]/g, '#');
  const path = h => {
    try { return (new URL(h, location.href).pathname.replace(/\/$/, '') || '/'); } catch (e) { return null; } };
  const shown = a => { const r = a.getBoundingClientRect(), s = getComputedStyle(a);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const links = [...document.querySelectorAll('a[href]')].filter(a => path(a.getAttribute('href')) === to && shown(a));
  // a link whose own query is part of where the click led ("?mode=signup"): "/login" alone opens another panel
  const wanted = new URLSearchParams(query || '');
  const fits = a => { const own = [...new URL(a.getAttribute('href'), location.href).searchParams];
    return own.length > 0 && own.every(([k, v]) => wanted.getAll(k).includes(v)); };
  // a button with those very words that isn't a link (it leads there by script): the agent presses it, never a
  // same-page link with other words ("Sign up" is a button, "Sign in" the only link to /login)
  const pressable = [...document.querySelectorAll('button, [role="button"], input[type="submit"], '
    + 'input[type="button"]')].some(b => b.tagName !== 'A' && shown(b) && norm(b.innerText || b.value) === label);
  const pick = links.find(a => norm(a.innerText) === label) || links.find(a => norm(a.innerText).includes(label))
    || (pressable ? null : (query ? links.find(fits) : links[0]));
  if (!pick) return {found: false};
  pick.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const words = (pick.innerText || '').replace(/\s+/g, ' ').trim();
  const spot = (POINT_ON)(pick);
  if (spot.covered) return {found: true, covered: true, label: words, by: spot.by};
  return {found: true, x: spot.x, y: spot.y, label: words};
})""".replace("(POINT_ON)", POINT_ON)
# a form's own button, by its words: inside a form, a filled one first (not a same-named link in the header)
PRESS_SUBMIT = r"""((label, values) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().replace(/[0-9]/g, '#');
  const key = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  // a closed panel's button has no innerText (not rendered): its textContent says its words
  const words = e => norm(e.innerText || e.textContent || e.value || e.getAttribute('aria-label'));
  const inputs = f => [...f.querySelectorAll('input, textarea')].filter(i => !['hidden', 'submit', 'button',
    'checkbox', 'radio', 'file'].includes(i.type));
  const filled = f => inputs(f).some(i => i.value);
  const named = [...document.querySelectorAll('form button, form input[type=submit], form [role=button]')]
    .filter(b => !b.disabled && words(b) === label);
  // a form inside a closed panel (a <details>): opened, as a person opens it before typing
  named.forEach(b => { for (let d = b.closest('details'); d; d = d.parentElement && d.parentElement.closest('details'))
    d.open = true; });
  const buttons = named.filter(shown);
  const pick = buttons.find(b => filled(b.closest('form'))) || buttons[0];
  if (!pick) return {found: false};
  // its own empty fields get the test's values, matched by the field's name, id, label or placeholder
  const typed = [];
  inputs(pick.closest('form')).filter(i => shown(i) && !i.value).forEach(i => {
    const lab = i.id && document.querySelector(`label[for="${CSS.escape(i.id)}"]`);
    const names = [i.name, i.id, i.getAttribute('aria-label'), i.placeholder, lab && lab.innerText, i.type].map(key);
    const k = Object.keys(values).find(v => names.includes(v)) ||
      Object.keys(values).find(v => names.some(n => n && (n.includes(v) || v.includes(n))));
    if (k === undefined) return;
    const set = Object.getOwnPropertyDescriptor(i.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype, 'value').set;
    set.call(i, values[k]);
    i.dispatchEvent(new Event('input', {bubbles: true}));
    i.dispatchEvent(new Event('change', {bubbles: true}));
    typed.push(k);
  });
  pick.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const spot = (POINT_ON)(pick);
  if (spot.covered) return {found: true, covered: true, label: words(pick), by: spot.by};
  return {found: true, x: spot.x, y: spot.y, label: words(pick), typed};
})""".replace("(POINT_ON)", POINT_ON)


def signing_up(flow):
    """The journey is about to sign up with its new account: the run's account book marks it as possibly made
    (account_book.AccountBook.reached), so the change's clean-up deletes it however the run ends."""
    book = account_book.from_env()
    email = flow.values.get("new_account_email") if flow.inbox else None
    if book is not None and email:
        book.reached(email)


def signed_up(flow):
    """The journey's sign-up step passed: the run's account book notes the app made the account
    (account_book.AccountBook.made)."""
    book = account_book.from_env()
    email = flow.values.get("new_account_email") if flow.inbox else None
    if book is not None and email:
        book.made(email)


SCROLL_POLL_S = 0.15  # page reads after a scroll (the usual 0.4 s x 3 steady reads held every scroll 1.2 s)
# point: where the input went on the screen (engine P26), for the replay's cursor (replay_cursor.py)
STEP_KEYS = ("step", "action", "kind", "text", "url", "probability", "confidence", "latency_ms", "executed_ms",
             "point")
WAIT_POLL_S = 20  # a step waiting for slow work (wait_s) looks at the page this often
# a failed step's page, read every WORKING_LOOK_S for WORKING_PROBE_S: still changing, never back to a text it showed
# before = the app is still working (a page that cycles, like a rotating headline or a carousel, isn't)
WORKING_PROBE_S = 20
WORKING_LOOK_S = 2
STEP_PAGE_CHARS = 800  # of each journey step's page kept in its result: the app's own words there, for later plans
PAGE_TEXT_CHARS = 1200  # of a failed test's last page kept in its result: what the page showed, as text


@dataclass
class FlowResult:
    name: str
    status: str
    passed: bool
    duration_ms: int
    expectations: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    decisions: int = 0
    input_tokens: int = 0
    usage: dict = field(default_factory=dict)  # {"engine": {...}, "text": {...}} as the gateway reported it
    final_url: str = ""
    error: str = ""
    warnings: list = field(default_factory=list)
    signed_in_by: str = ""  # a sign-in that passed: "email" (it opened the emailed link) or "password" (typed)
    screenshot: str = ""
    source: str = ""
    persona: str = ""
    expect_denied: bool = False
    session_saved: bool = False
    video: str = ""  # replay MP4, when recording was requested and ffmpeg is available
    timeline: list = field(default_factory=list)  # [{ms, label}] captions for the replay
    cause: str = ""  # why it did not pass: app | machine (see verdict.py)
    poster: str = ""  # where the test ended (the replay's last frame), for passing tests too
    consent: str = ""  # how it answered a cookie banner before acting ("Cookie banner: chose “Only required”")
    challenge: str = ""  # a security check (CAPTCHA) found on the page where it stopped, e.g. "Cloudflare Turnstile"
    server_answer: dict = field(default_factory=dict)  # a failed test whose last page's server said 5xx/429
    proves_nothing: bool = False  # a form test whose checks already held before it did anything
    signed_out: str = ""  # why the page it ended on says its user type is signed out (its session expired mid-run)
    page_text: str = ""  # a failed test: the start of its last page's text (secrets redacted), for those without video
    blocked_saves: list = field(default_factory=list)  # a failed test: saves the write-guard stopped on its last page
    # a journey (flow.steps): [{step, goal, reload, status, passed, held_before, expectations, picture, at_ms}]
    steps_judged: list = field(default_factory=list)
    failed_step: int = 0  # a journey that failed: the number of the step that stopped it (1-based)
    steps_total: int = 0  # a journey: how many steps it has (a stopped one judged fewer)
    # the values the text model typed and whether each field accepted them (typed_values.py): [{field, model,
    # accepted, cause, message}]
    typed_values: list = field(default_factory=list)
    # a sign-in that failed with its form still on screen: what the form says now that it didn't say at first (the
    # app's own words, "Wrong password", or the browser's check of a field), from FORM_WORDS
    signin_said: list = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


# what the page's own server answered, read from the page (the Navigation Timing API): black-box, any app
# ...and its own requests: one of them refused with 429 (too many requests) means the app is rate-limiting this
# visitor (a chat: "That is a lot of questions in one go"): nothing to judge, try later
SERVER_ANSWER = ("(() => { const nav = performance.getEntriesByType('navigation')[0];"
                 " const limited = performance.getEntriesByType('resource').find(r => r.responseStatus === 429"
                 " && new URL(r.name).hostname === location.hostname);"
                 " return {status: (nav && nav.responseStatus) || 0, title: document.title || '',"
                 " limited: limited ? new URL(limited.name).pathname : ''}; })()")


def server_answer(browser):
    """{status, title} when the page's server didn't answer it (5xx, or 429: too many requests), else {}."""
    try:
        seen = browser.evaluate(SERVER_ANSWER)
    except Exception:
        return {}
    if not isinstance(seen, dict):
        return {}
    status = int(seen.get("status") or 0)
    if status >= 500 or status == 429:
        return {"status": status, "title": (seen.get("title") or "")[:120]}
    if seen.get("limited"):  # the page answered, one of its own requests was refused: too many requests
        return {"status": 429, "title": f"too many requests to {str(seen['limited'])[:80]}"}
    return {}


# a server that couldn't be reached properly (a gateway, an overload, a deploy in progress, too many requests):
# nothing to judge. Any other 5xx, a 500 above all, is the app's own code failing on that page: broken.
UNREACHABLE = (429, 502, 503, 504)


def crash_message(url, answer):
    host = urlparse(url).hostname or "the app"
    said = f"{answer['status']} {answer['title']}".strip() if answer.get("title") else str(answer["status"])
    return f"{host} failed on this page with a server error (it said “{said}”)."


def unavailable_message(url, answer):
    host = urlparse(url).hostname or "the app"
    said = f"{answer['status']} {answer['title']}".strip() if answer.get("title") else str(answer["status"])
    return f"{host} didn’t answer this page (it said “{said}”), so the test couldn’t check it."


def page_excerpt(text):
    """A page's text as a reader needs it to see what went wrong: blank lines and runs of spaces folded, cut."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    folded = "\n".join(line for line in lines if line)
    return folded if len(folded) <= PAGE_TEXT_CHARS else folded[:PAGE_TEXT_CHARS].rstrip() + "…"


def email_signin_failed(result):
    """Why signing in by email (an EveryRole inbox) didn't work, from the step it stopped at."""
    step = next((s for s in result.steps_judged if s["step"] == result.failed_step), None)
    if step is None:
        return "Signing in by email didn't work."
    details = "; ".join(e.get("detail", "") for e in step.get("expectations") or [] if not e.get("passed"))
    return f"Signing in by email stopped at “{step['goal'][:80]}”" + (f": {details}." if details else ".")


def no_password_form(url):
    """Why a sign-in that never showed a password field stopped, and the two ways out."""
    where = f"It stopped on {url} and " if url else ""
    return (f"{where}no password field ever appeared. If your sign-in form is on another page (an admin sign-in "
            "page, a page behind a welcome screen), set that page as the sign-in page. If you sign in through "
            "another site (like Google or GitHub) or an emailed link or code, sign in yourself once and save that "
            "session as the user type's `saved_signin`: EveryRole reuses it.")

PASSWORD_FIELDS = r"""(() => [document, ...[...document.querySelectorAll('iframe')]
  .map(f => { try { return f.contentDocument; } catch (e) { return null; } }).filter(Boolean)]
  .flatMap(d => [...d.querySelectorAll('input[type="password"]')])
  .filter(e => e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true).length)()"""  # noqa: E501

# what a sign-in form says, read around its visible password field (its <form>, else the nearest part of the page
# that holds a button too): the browser's own message for a field that refuses its value ("Please lengthen this
# text to 8 characters or more"), the form's own sentences (not its labels, buttons, links or fields), and the page's
# alerts. Read when the form first shows and again when a sign-in failed: what is new is what the app answered.
FORM_WORDS = r"""(() => {
  const visible = e => e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true;
  const field = [...document.querySelectorAll('input[type="password"]')].find(visible);
  if (!field) return [];
  let box = field.form;
  for (let e = field.parentElement; !box && e && e !== document.body; e = e.parentElement)
    if (e.querySelector('button,[type="submit"],[role="button"]')) box = e;
  box = box || field.parentElement;
  const words = [];
  const add = t => {
    t = (t || '').replace(/\s+/g, ' ').trim().slice(0, 160);
    if (t.length > 1 && !words.includes(t)) words.push(t);
  };
  for (const input of box.querySelectorAll('input,select,textarea'))
    if (visible(input) && input.willValidate && !input.validity.valid) add(input.validationMessage);
  const skip = 'label,button,a,option,select,input,textarea,legend,[role="button"]';
  const walk = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
  for (let node = walk.nextNode(); node; node = walk.nextNode()) {
    const parent = node.parentElement;
    if (parent && !parent.closest(skip) && visible(parent)) add(node.textContent);
  }
  for (const alert of document.querySelectorAll('[role="alert"],[aria-live="assertive"],[aria-live="polite"]'))
    if (visible(alert)) add(alert.innerText);
  return words.slice(0, 12);
})()"""


def form_words(browser):
    """FORM_WORDS on the page open now ([] when it has no sign-in form or can't be read)."""
    try:
        found = browser.evaluate(FORM_WORDS)
    except Exception:
        return []
    return [w for w in found if isinstance(w, str)] if isinstance(found, list) else []


PAGE_CONTROLS = r"""(() => {
  const visible = e => e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true;
  const text = e => (e.getAttribute('aria-label') || e.innerText || e.value || e.getAttribute('placeholder') ||
    e.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const labelOf = e => {
    const id = e.getAttribute('id');
    const label = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
    return label ? label.innerText.replace(/\s+/g, ' ').trim() : '';
  };
  const found = [...document.querySelectorAll('a[href],button,[role="button"],[role="link"],[role="tab"],' +
    'input:not([type="hidden"]),textarea,select')].filter(visible).flatMap(e => [text(e), labelOf(e)]);
  return JSON.stringify([...new Set(found.filter(Boolean))]);
})()"""


# what the visible fields hold (never a password): a draft restored into a form is on the page too
FIELD_VALUES = r"""(() => {
  const NOT_TEXT = ['checkbox', 'radio', 'submit', 'button', 'file', 'range', 'color'];
  const values = [...document.querySelectorAll('input:not([type=hidden]):not([type=password]),textarea,select')]
    .filter(e => e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true)
    .filter(e => !NOT_TEXT.includes((e.type || '').toLowerCase()))
    .map(e => e.tagName === 'SELECT' ? (e.selectedOptions[0] || {}).text || '' : e.value || '')
    .filter(v => v.trim());
  return JSON.stringify(values);
})()"""


def read_facts(browser, page):
    """Fresh URL and full rendered text from the browser; labels of every visible control on the whole page
    (not only the part in view), plus the latest observation's labels."""
    url = browser.evaluate("location.href") or page.get("url", "")
    text = browser.evaluate("document.body ? document.body.innerText : ''") or ""
    try:
        filled = json.loads(browser.evaluate(FIELD_VALUES) or "[]")
    except Exception:  # an old page or a test double without it: the text alone
        filled = []
    if isinstance(filled, list) and filled:
        text = text + "\n" + "\n".join(str(v) for v in filled)
    try:
        controls = json.loads(browser.evaluate(PAGE_CONTROLS) or "[]")
    except (TypeError, ValueError):
        controls = []
    labels = tuple(dict.fromkeys([*(a.get("label", "") for a in page.get("actions", [])), *controls]))
    passwords = browser.evaluate(PASSWORD_FIELDS) or 0
    return assertions.PageFacts(url=url, text=text, labels=labels, blocked=tuple(writeguard.blocked(browser)),
                                passwords=passwords)


def _not_kept(flow):
    """What a journey types that the "kept" check never looks for: secrets, and the new account's own details
    (typed into the sign-up form, not the visitor's work)."""
    own = [v for k, v in flow.values.items() if k.startswith("new_account_")]
    return tuple(flow.secrets.values()) + tuple(own)


def _agent_factory(url, goal, **hooks):
    from jev_ultrafast import Agent

    return Agent(url, goal, **hooks)


def _outbound_filters():
    from jev_ultrafast import model

    return model.OUTBOUND_FILTERS


def _release_all(pace, tickets):
    while tickets:
        pace.release(tickets.pop())


class FlowRunner:
    def __init__(
        self,
        *,
        agent_factory=_agent_factory,
        facts_reader=read_facts,
        outbound_filters=_outbound_filters,
        value_chooser=ValueChooser,
        clock=time.monotonic,
        sleep=time.sleep,
        poll_s=0.25,
        wait_stable=wait_until_stable,
        pace=None,
    ):
        self._agent_factory = agent_factory
        self._pace = pace  # the job's network pace (see pace.py): the start page waits for room to load
        self._facts = facts_reader
        self._filters = outbound_filters
        self._value_chooser = value_chooser
        self._clock = clock
        self._sleep = heartbeat.beating(sleep)  # every wait is a sign of life (heartbeat.py)
        self._poll_s = poll_s
        self._wait_stable = wait_stable

    def _release(self, tickets):
        if self._pace:
            _release_all(self._pace, tickets)

    def run(self, flow, output_dir=None, *, session_state=None, save_session_to=None, record_dir=None, site=None,
            signed_out=None):
        """`site`: every address of the app, so a saved session keeps what each part stored. `signed_out`: facts ->
        why the page says the user is signed out ("" when it doesn't), checked on the page a failed test ended on
        (see signin.SignedOut)."""
        started = self._clock()
        setup_problem = False  # set when a person must change something (a password field with no value)
        self._passwords_seen = 0  # the most password fields any page of this run showed
        # a sign-in: what its form said when it first showed a password field, to tell what it says after (FORM_WORDS)
        self._form_words = [] if flow.is_login else None
        self._origins = tuple(f"{urlparse(u).scheme}://{urlparse(u).netloc}" for _, u in site.parts) if site else ()
        self._ignore_saves = flow.ignore_saves
        self._held_before_acting = False
        self._last_facts = None  # the latest page read: a failed test says what its last page showed
        self._journey_open = False  # a journey stopped before its last step: it can't be "recovered" into a pass
        self._typed, self._typed_before = [], ()  # what the journey typed (never secrets); before the current step
        self._typing = TypedValues()  # the text model's values and whether their fields accepted them
        self._seen_paths, self._seen_before = set(), frozenset()  # pages (paths) read so far; before the current step
        self._last_step = True  # judging a journey's last step (or a single test): "kept" is judged wherever it ends
        # a journey's inbox: a new account's (new for this run), or a test account's that signs in by email, where
        # what came before this sign-in is old (an earlier link): only what arrives now counts
        self._inbox = flow.inbox
        self._emails_opened = {m["id"] for m in mailbox.messages(flow.inbox)} if flow.inbox and flow.is_login else set()
        self._by_email = False  # a sign-in that opened the link the app emailed (its proof, not a password form)
        # the password it signs in or signs up with: typed, the account went the password way
        self._password = flow.values.get("password") or flow.values.get("new_account_password") or None
        self._password_typed = False
        if flow.block_writes and flow.submit_settle_s > flow.settle_s:
            flow = replace(flow, settle_s=flow.submit_settle_s)  # submits trigger slow work before saving
        redactor = Redactor(flow.secrets)
        guard = Guard(flow.forbid, flow.allowed_hosts)
        checks = assertions.build(flow.expect)
        self._judging = checks  # the checks the result is judged on: a journey's current step's
        filters = self._filters()
        filters.append(redactor.apply)
        result = FlowResult(
            name=flow.name, status="error", passed=False, duration_ms=0, source=flow.source,
            persona=flow.persona, expect_denied=flow.expect_denied,
        )
        browser_options = {"isolated": True}
        tickets = []  # the start page's place in the job's network pace, held until it has loaded
        # restore the session (if any), then track in-flight requests so "loaded" means really loaded
        browser_options["prepare"] = chain(
            (lambda browser: tickets.append(self._pace.acquire(flow.start))) if self._pace else None,
            restorer(session_state) if session_state else None,
            track_network,
            writeguard.block_writes if flow.block_writes else None,  # form-only: nothing can be saved
            inject.from_env(),  # benchmarking only: a planted bug (everyrole run --inject)
        )
        # the first decision is made on the loaded start page, not on a spinner or skeleton
        def settle(browser):
            try:
                self._wait_stable(browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                                  sleep=self._sleep)
            finally:
                self._release(tickets)

        browser_options["settle"] = settle
        recording = {"record_dir": str(record_dir)} if record_dir else {}
        self._record_dir = record_dir  # a step that waits for slow work adds a picture each time it looks
        agent = None
        self._covers = []  # windows from other sites laid over the page: [{step, by, closed}]
        self._step_number = 0  # a journey sets it per step
        outcomes = []
        try:
            agent = self._agent_factory(
                flow.start,
                flow.goal,
                max_steps=flow.max_steps,
                text_provider=self._recording(chooser := self._value_chooser(flow.values,
                                                                             tuple(flow.secrets.values())),
                                              _not_kept(flow)),
                before_execute=self._before_typing(guard.check_action),
                browser_options=browser_options,
                stop_needs_fresh_page=False,  # EveryRole's own checks decide; "done"/"blocked" make no input
                # a sign-in never sends its form with the account or password field still empty (P27): the model pressed
                # "Log in" on an empty form, again and again (2026-11-18)
                **({"fill_before_submit": True} if flow.is_login else {}),
                after_action=lambda browser: self._after_action(browser, flow),
                **recording,
            )
            self._covered = None
            self._answer_consent(agent, result)
            self._close_cover(agent.browser, flow)
            if flow.steps:
                self._chooser = chooser
                result.status, outcomes = self._journey(agent, flow, guard, output_dir, result)
            else:
                self._held_before_acting = flow.acts and self._holds_now(agent, checks)
                result.status, outcomes = self._drive(agent, flow, guard, checks, started)
        except GuardViolation as exc:
            result.status, result.error = exc.status, str(exc)
        except BudgetExceeded as exc:  # the goal was not reached within the test's budget: a result, not a crash
            result.status, result.error = "budget_exceeded", str(exc) + _stale_note(agent)
        except SecretField as exc:  # a password field and no value for it: a person must add one
            result.status, result.error = "error", str(exc)
            setup_problem = True
        except Exception as exc:  # provider/browser failures; expectations below may still prove success
            result.status, result.error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if agent is not None:
                    self._cover_at_end(agent)
                    outcomes = self._final_outcomes(agent, self._judging, outcomes)
                    self._finish(agent, result, outcomes, output_dir, flow)
                    if flow.is_login and not result.passed and self._form_words:
                        # read while its browser is open: after agent.close() every read came back empty, so a
                        # refused sign-in never said the app's words ("Invalid email or password.")
                        result.signin_said = [w for w in form_words(agent.browser) if w not in self._form_words][:3]
                    if not result.passed and signed_out is not None:
                        result.signed_out = self._signed_out(agent, signed_out)
                    if result.passed and save_session_to:
                        self._save_session(agent, result, save_session_to)
                    if record_dir:
                        self._final_frame(agent, record_dir)
            finally:
                self._release(tickets)  # a start page that never loaded gives its place back
                filters.remove(redactor.apply)
                if agent is not None:
                    try:
                        agent.close()
                    except Exception as exc:
                        result.warnings.append(f"close failed: {type(exc).__name__}: {exc}")
        if result.passed and flow.acts and self._held_before_acting and not flow.changes_password and not flow.steps:
            # (a password change is proven by signing in with the new password afterwards, see suite.py)
            result.proves_nothing = True  # it passed, but its checks held before it filled anything in
            result.warnings.append("its checks already held before it filled anything in: it doesn't prove the task")
        result.duration_ms = round((self._clock() - started) * 1000)
        # An exception inside the run (browser, model provider) says nothing about the app.
        result.cause = "" if result.passed else (verdict.MACHINE if result.status == "error" else verdict.APP)
        if result.cause == verdict.APP and result.status == "failed_expect" and verdict.reworded(result.expectations):
            result.cause = verdict.WORDING  # it works, it says it differently now: not a failure
        if flow.is_login and result.cause == verdict.APP and result.status == "blocked" and not result.steps \
                and self._passwords_seen:
            # the step engine gave up before trying anything, on a page that shows the sign-in form: its mistake
            result.cause = verdict.MACHINE
            result.error = "The step engine stopped before trying to sign in, with the sign-in form on screen."
        if flow.is_login and result.passed:
            result.signed_in_by = "email" if self._by_email and not self._password_typed else (
                "password" if self._password_typed else "")
        if flow.is_login and result.passed and not self._passwords_seen and not self._by_email:
            # "no password asked" holds on a page that never had a form (a welcome screen): not signed in
            result.passed, result.status = False, "failed_expect"
            result.cause, result.error = verdict.SIGNIN, no_password_form(result.final_url)
        cover = None if result.passed else self._cover_left(result, flow)
        if cover:
            # the page was under another site's window when its checks were read: they never had a chance, so
            # neither "your app is broken" nor "EveryRole's guess of words" (automationexercise.com, 2026-11-17)
            at = f"At step {cover['step']}, a" if flow.steps and cover["step"] else "A"
            result.error = (f"{at} window from {cover['by'] or 'another site'} (most often an ad) covered the whole "
                            "page and EveryRole couldn't close it, so it couldn't see the page under it or act on it. "
                            "The replay shows it.")
        covered = getattr(self, "_covered", None)
        if not result.passed and not cover and covered and result.failed_step == covered["step"]:
            # something fixed on screen covers the link where a person would click it: say what, never "stayed on
            # the home page". A cookie banner EveryRole won't accept for anyone is the test's limit, not the app's
            banner = bool(result.consent) and "left open" in result.consent
            result.error = (f"The link “{covered['label']}” is covered by “{covered['by']}”, which stays on top of the "
                            "page, so it can't be clicked" + (": a cookie banner that only offers to accept (EveryRole "
                                                              "never accepts cookies for you)." if banner else "."))
            setup_problem = banner
        if not result.passed and not setup_problem and not cover and self._stale_step(result):
            # its checks held before the step acted and failed after: they describe the page it started on, so they
            # say nothing about the app. The test needs other checks, never "your app is broken"
            setup_problem = True
            step = self._stale_step(result)
            result.error = (f"Step {step['step']} can't be proven by its checks: every one of them already showed "
                            "before the step did anything (words on every page, like the menu or the footer, or the "
                            "page it came from). Check something only the page it leads to shows, such as its own "
                            "heading.")
        guessed = None if result.passed or setup_problem or cover else self._guessed_step(result)
        if guessed:
            # its checks were the planner's guess (words never seen on the app): the guess is wrong, not the app
            setup_problem = True
            if any(e.get("kind") == "open_email" for e in guessed.get("expectations") or []):
                result.error = (f"Step {guessed['step']} expected an email to open (EveryRole’s guess): "
                                f"{guessed['expectations'][0].get('detail') or 'none came'}. Your app goes on "
                                "another way there: the page it stopped on shows how.")
            elif guessed.get("status") in ("blocked", "budget_exceeded"):
                # the step was never done: its checks were never put to the test, so they aren't the ones to blame
                # (a trial, 2026-11-18: "Add to cart" never pressed, yet "the checks were EveryRole's guess")
                result.error = (f"Step {guessed['step']} wasn’t done: EveryRole couldn’t find how to "
                                f"“{guessed['goal']}” on the page within its tries, so what it checks for was never "
                                "put to the test. The replay shows what it tried.")
            elif guessed.get("page_unchanged"):
                # the words may well be real: the step's action changed nothing, so they never had a chance to show
                # (a new customer's trial, 2026-11-17: "Added!" after an Add to cart click that did nothing)
                result.error = (f"Step {guessed['step']} changed nothing on the page: after “{guessed['goal']}” the "
                                "page stayed exactly as it was, so what it checks for never showed. The replay and "
                                "the page it stopped on show where it stayed.")
            else:
                result.error = (f"Step {guessed['step']}’s checks were EveryRole’s guess of words it had never seen "
                                "on your app, and the page shows something else: the test needs the page’s real "
                                "words there (the page it stopped on shows them).")
        working = None if result.passed or setup_problem or cover else self._working_step(result)
        if working:
            # slow work no one said to wait for: the test must wait (the walk adds it), never "your app is broken"
            setup_problem = True
            result.error = (f"Step {working['step']}: your app was still working when the step’s time was up (its "
                            "page kept changing on its own): the test needs to wait for it there.")
        if setup_problem and not result.passed:
            result.cause = verdict.SETUP
        if cover:  # EveryRole couldn't check: nothing is said about the app
            result.cause = verdict.MACHINE
        if result.challenge and not result.passed:
            from .challenge import message

            result.cause, result.error = verdict.SIGNIN, message(result.challenge)
        elif result.server_answer and not result.passed and result.server_answer["status"] in UNREACHABLE:
            result.cause = verdict.UNAVAILABLE
            result.error = unavailable_message(result.final_url, result.server_answer)
        elif result.server_answer and not result.passed:  # its code crashed there (500...): broken, never excused
            result.cause = verdict.APP
            result.error = crash_message(result.final_url, result.server_answer)
        elif flow.is_login and flow.inbox and result.cause == verdict.APP:
            result.cause, result.error = verdict.SIGNIN, email_signin_failed(result)
        elif flow.is_login and result.cause == verdict.APP and not self._passwords_seen:
            # no password field ever appeared: the form is on another page than the one set, or the sign-in goes
            # through another site (Google, GitHub...), an emailed link or code. Never an app failure.
            result.cause, result.error = verdict.SIGNIN, no_password_form(result.final_url)
        if record_dir and agent is not None:
            self._make_video(result, record_dir)
        return self._redacted(result, redactor)

    PAGE_POSITION = "[scrollY, innerHeight, document.documentElement.scrollHeight]"

    def _scroll_down(self, browser):
        """Scroll one screen with a real mouse wheel (smooth-scroll libraries ignore window.scrollBy).
        -> True if the page actually moved."""
        before, height, total = browser.evaluate(self.PAGE_POSITION)
        if before + height >= total - 4:
            return False
        browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=height // 2, y=height // 2, deltaX=0,
                     deltaY=round(height * 0.8))
        self._wait_stable(browser, max_s=2, poll_s=self._poll_s, clock=self._clock, sleep=self._sleep)
        return browser.evaluate(self.PAGE_POSITION)[0] > before

    def _scroll_to_top(self, browser):
        """Back to the top with a real mouse wheel (headers, menus and cart icons live there).
        -> True if the page actually moved."""
        before, height, _total = browser.evaluate(self.PAGE_POSITION)
        if before <= 4:
            return False
        browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=height // 2, y=height // 2, deltaX=0,
                     deltaY=-round(before + height))
        self._wait_stable(browser, max_s=2, poll_s=self._poll_s, clock=self._clock, sleep=self._sleep)
        return browser.evaluate(self.PAGE_POSITION)[0] < before

    def _resume_after_scroll(self, agent, flow, scrolls, may_look=True, may_return=True):
        """The agent gave up. If the page has changed since it decided (a slow navigation landed), let it look
        again; otherwise, if the page continues below the fold (it only sees what is on screen), scroll; once it
        can't (or may not) scroll further down, go back to the top once: what it needs may be up there.
        -> "look" | "scroll" | "top" | None (stop). Pages with live content (tickers, rotating headlines) change
        all the time, so a second give-up in a row never just looks again: "the page changed" must not use up
        the scrolls."""
        if agent.snapshot()["status"] != "blocked" or (scrolls >= flow.scroll_retries and not may_return):
            return None
        try:
            self._wait_stable(agent.browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                              sleep=self._sleep)
            closed = self._close_cover(agent.browser, flow, stuck=True)  # a full-page ad over the page it gave up on
            seen = agent.state.get("page", {}).get("fingerprint")
            fresh = agent.browser.observe(screenshot=agent.screenshots)
            if (may_look or closed) and seen and fresh.get("fingerprint") and fresh["fingerprint"] != seen:
                agent.state["page"], agent.state["status"] = fresh, "ready"
                return "look"
            if scrolls < flow.scroll_retries and self._scroll_down(agent.browser):
                how = "scroll"
            elif may_return and self._scroll_to_top(agent.browser):
                how = "top"
            else:
                return None
            self._wait_stable(agent.browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                              sleep=self._sleep)
            agent.state["page"] = agent.browser.observe(screenshot=agent.screenshots)
            agent.state["status"] = "ready"
            return how
        except Exception:
            return None

    def _drive(self, agent, flow, guard, checks, started):
        scrolls, looked, returned = 0, False, False
        while True:
            result = self._drive_once(agent, flow, guard, checks, started)
            if result is not None:
                return result
            how = self._resume_after_scroll(agent, flow, scrolls, may_look=not looked, may_return=not returned)
            if how is None:
                break
            looked = how == "look"
            scrolls += how == "scroll"
            returned = returned or how == "top"
        return self._conclude(agent, flow, checks)

    def _drive_once(self, agent, flow, guard, checks, started):
        """Run the agent until it stops. -> (status, outcomes) if the flow is decided, else None. It stops early once
        every check holds, unless the checks already held on the page it started on (a form test that proves
        nothing): then it does the whole task and is flagged (see run)."""
        outcomes = []
        for state in agent.run():
            guard.check_url(state["page"]["url"])
            outcomes = assertions.evaluate(checks, self._read(agent.browser, state["page"]))
            if all(o.passed for o in outcomes) and not flow.expect_denied and not self._held_before_acting:
                settled = self._settled_outcomes(agent, checks, flow.settle_s)
                if settled and all(o.passed for o in settled):
                    return "passed", settled  # early stop: the success condition holds on the loaded page
                outcomes = settled or outcomes  # it only held while loading; keep going
            if self._clock() - started > flow.timeout_s:
                return "budget_exceeded", outcomes
        return None

    def _journey(self, agent, flow, guard, output_dir, result):
        """A journey (flow.steps), in one browser: each step's goal goes to the same agent (it keeps the page, the
        session and what it did so far), then the step's checks are judged on a settled page, after a reload when
        the step asks for one. The first step that fails stops the journey. -> (status, outcomes of the last
        judged step)."""
        outcomes = []
        result.steps_total = len(flow.steps)
        kept_later = False
        before_facts = None  # the page as the previous step found it, before it acted
        for number, step in enumerate(flow.steps, 1):
            self._step_number = number
            self._dialogs_from = len(self._page_dialogs(agent.browser))  # its own alerts: the ones shown from here on
            heartbeat.beat()
            # "nothing lost" not judged yet (the visitor wasn't back on the page it was typed on): this step judges it
            expect = step["expect"] if not kept_later or any("kept" in c for c in step["expect"]) else \
                [*step["expect"], {"kept": True}]
            checks = assertions.build(expect)
            self._judging = checks
            self._last_step = number == len(flow.steps)
            self._journey_open = number < len(flow.steps)  # an exception here leaves the next steps unproven
            self._typed_before, self._seen_before = tuple(self._typed), frozenset(self._seen_paths)
            status, held, by_next = "passed", False, False
            page_before = {}  # the page as the step found it, to tell whether its action changed anything
            # did its checks hold before the step did anything, its email included? Then they can't prove it
            # (an email's link opened before the goal is the step's own doing: an app's welcome page)
            at_start = bool(step["goal"]) and step["open_email"] and self._holds_now(agent, checks)
            # a sign-in (or a sign-up) that may go by email waits for it unless it typed the password and the page
            # already shows what the email would lead to: then the app sends none, and its email checks are dropped
            # (Conduit, 2026-11-17: the main path waited for a confirmation email the app never sends)
            on_page = [c for c in checks if not isinstance(c, assertions.EmailReceived)]
            unneeded = (step["open_email"] and step.get("email_if_needed") and self._password_typed and on_page
                        and self._holds_now(agent, on_page))
            if unneeded:
                checks = self._judging = on_page
            elif step["open_email"]:
                missing = self._open_email(agent, flow, step["email_wait_s"])
                if missing:  # no email came: the step can't go on, and says why
                    status, outcomes = "failed_expect", [missing]
            ignored = []
            if step["sign_up"] and status == "passed":
                # from here on the account may exist on the app: the book says so now, not when the test ends, so a
                # run stopped or killed mid-sign-up still has it deleted (staging, 2026-11-17: Stop, and a change
                # that couldn't go on, left "EveryRole Test" on the app with no clean-up)
                signing_up(flow)
            # an email the page shows unneeded: its step (going to the email) is done already, judged on this page
            if step["goal"] and status == "passed" and not unneeded:
                self._next_goal(agent, flow, step["goal"] + step.get("hint", ""), first=number == 1)
                facts = self._facts_now(agent)
                # a test that only reads the page a link led to (its way in) must tell that page apart from the one
                # the link was on: there its checks already hold before its own (reading) step, by design
                reference = before_facts if step.get("reached_by_previous") and not flow.acts and before_facts \
                    else facts
                before_facts = facts
                held, checks, ignored = self._discriminating(checks, reference)
                if step.get("submit"):  # the form's own button: a save tried already (the agent pressed it while
                    held = False  # filling) is the same proof, never "it held before the step"
                self._judging = checks
                at_start = held if not step["open_email"] else at_start
                # no early stop when the checks held before acting, or when they are judged after a reload, or on a
                # sign-up: its form's own fields would stop it before anything is typed (an app's sign-up)
                self._held_before_acting = held or step["reload"] or step["sign_up"]
                page_before = self._page_seen(agent)
                clicked = (self._click_link(agent, flow, guard, step["click"], checks) if step.get("click") else
                           self._press_submit(agent, flow, step["submit"], checks) if step.get("submit") else None)
                try:
                    status, outcomes = clicked or self._drive(agent, flow, guard, checks, self._clock())
                except BudgetExceeded as exc:  # out of steps: the step's checks still decide
                    status, outcomes = "budget_exceeded", []
                    result.warnings.append(f"step {number}: {exc}")
                if step.get("checked_by_next") and not checks and status == "failed_expect":
                    status = "passed"  # finished, not stuck: its proof is the next step (the email it asked for)
                if step["sign_up"] and status == "passed" and self._form_still_there(agent):
                    # the agent finished and the sign-up form is still on screen: no account was made
                    status = "failed_expect"
                    outcomes = [*outcomes, assertions.Outcome("sign_up", "a new account made", False,
                                                              "the sign-up form is still on screen")]
                    self._journey_open = True  # failed for more than its checks: a fresh read can't pass it
            # slow work: its checks may hold only minutes later; wait for them without acting
            waits = step["wait_s"] and status != "forbidden_action" and not (step["goal"] and status == "passed")
            if waits:
                outcomes = self._wait_for(agent, checks, step["wait_s"], reload=step["reload"], flow=flow)
                status = "passed" if outcomes and all(o.passed for o in outcomes) else "failed_expect"
            if step["reload"] and status != "forbidden_action" and not waits:
                self._reload(agent, flow)
            only_email = step["open_email"] and (not step["goal"] or unneeded) and status == "passed"  # judged on the
            # opened page
            if not waits and (step["reload"] or status == "budget_exceeded" or only_email):
                outcomes = self._settle(agent, checks, flow.settle_s)
                if outcomes and all(o.passed for o in outcomes):
                    status = "passed"
                elif status == "passed":
                    status = "failed_expect"
            # judged once its status is final: a step out of moves passes on a fresh read too (a checkout
            # step: it wandered back home, where the pricing page's "$12, one time" also shows).
            # A reload step proves what survives it; EveryRole's own sign-in finishes "whatever is left"
            if at_start and status == "passed" and step["goal"] and not step["reload"] and not flow.is_login:
                # its checks held before it did anything: they can't tell it did it. In the middle of a journey
                # (typing into a field changes nothing else) the next step proves it; the last step must prove
                # the journey itself: "open the checkout" checked by the app's name on every page passed on an
                # app with no checkout
                if number == len(flow.steps):
                    status = "failed_expect"
                    self._journey_open = True
                else:
                    by_next = True
            if step["sign_up"] and status == "passed":
                signed_up(flow)
            result.steps_judged.append({
                "step": number, "goal": step["goal"] or ("Open the email's link" if step["open_email"]
                                                         else "Wait for it to finish" if step["wait_s"]
                                                         else "Reload the page"), "reload": step["reload"],
                "unsure": step.get("unsure", False), "status": status, "passed": status == "passed",
                # failed while the app was still at work (its page kept changing): slow work no one said to wait for
                "still_working": status in ("failed_expect", "budget_exceeded") and not step["wait_s"]
                and self._still_working(agent),
                "held_before": at_start and not by_next,
                # checks that held before the step too (the site's menu, header, footer): they prove nothing about
                # it, so it was judged on the others
                "not_judged": ignored,
                # its email wasn't needed: the app had already signed the account in (said so, never shown as checked)
                "email_skipped": bool(unneeded) and status == "passed",
                "proven_by_next": by_next,  # its own checks held before it: what comes after proves it
                "expectations": [o.as_dict() for o in outcomes],
                "picture": self._step_picture(agent, output_dir, result.name, number),
                # what the app showed there, its alerts included: later plans know it
                "page": (page_after := self._with_alerts(self._page_seen(agent), agent)),
                # its action left the page exactly as it found it (same address, same words): None when unknown
                "page_unchanged": (page_before == page_after) if page_before and page_after else None,
                "at_ms": agent.snapshot().get("elapsed_ms") or 0})
            if status != "passed":
                result.failed_step = number
                return status, outcomes
            kept_later = any(o.kind == "kept" and o.detail == assertions.KEPT_LATER for o in outcomes)
        self._journey_open = False
        return "passed", outcomes

    @staticmethod
    def _next_goal(agent, flow, goal, first):
        """Give the agent its next step's goal; it keeps the browser and its history, with a fresh step budget."""
        if first:
            return  # the agent was made with the first step's goal
        state = agent.state
        state.update(goal=goal, plan=[goal], plan_index=0, status="ready", decision=None)
        used = max(len(state["history"]), -(-len(state["decisions"]) // 2))
        agent.max_steps = used + flow.max_steps

    @staticmethod
    def _page_seen(agent):
        """The page a step was judged on: {url, text} (its first STEP_PAGE_CHARS), or {} when it can't be read."""
        try:
            page = agent.browser.observe(screenshot=False)
            return {"url": page.get("url", ""), "text": (page.get("text") or "")[:STEP_PAGE_CHARS]}
        except Exception:
            return {}

    def _with_alerts(self, page, agent):
        """The page a step was judged on, with the browser alerts it showed during the step (their words are the
        app's own: "Product added.")."""
        said = self._page_dialogs(agent.browser)[getattr(self, "_dialogs_from", 0):]
        if not page or not said:
            return page
        return {**page, "text": (page["text"] + "".join(f"\nBrowser alert: {m}" for m in said))[:STEP_PAGE_CHARS]}

    @staticmethod
    def _form_still_there(agent):
        """A password field is still on screen (the sign-up form after the agent finished)."""
        try:
            return bool(agent.browser.evaluate(PASSWORD_FIELDS))
        except Exception:
            return False

    def _still_working(self, agent):
        """The page, read every WORKING_LOOK_S for WORKING_PROBE_S with no one acting, keeps showing new text: the
        app is still working on something (a film being made, a report). Text that comes back after another one
        replaced it is the page cycling (a rotating headline: a signed-in home page was taken for
        work, so the walks added waits instead of fixing the step's checks); text that never changes has settled.
        Measured on the page itself, never from its words."""
        try:
            seen = [agent.browser.observe(screenshot=False).get("text", "")]
            for _ in range(WORKING_PROBE_S // WORKING_LOOK_S):
                self._sleep(WORKING_LOOK_S)
                text = agent.browser.observe(screenshot=False).get("text", "")
                if text == seen[-1]:
                    continue  # slow work shows its next state later
                if text in seen:
                    return False  # cycling
                seen.append(text)
            return len(seen) > 1
        except Exception:
            return False

    @staticmethod
    def _working_step(result):
        """The journey step that failed while the app was still working on it, else None."""
        step = next((s for s in result.steps_judged if s["step"] == result.failed_step), None)
        return step if step and step.get("still_working") else None

    @staticmethod
    def _guessed_step(result):
        """The journey step that failed on checks the planner guessed (`unsure`), else None: its checks unmet, or the
        visitor lost on the way there (blocked, out of steps: looking for a "Pricing" page the
        app hasn't). A forbidden button is never excused."""
        step = next((s for s in result.steps_judged if s["step"] == result.failed_step), None)
        lost = ("failed_expect", "blocked", "budget_exceeded")
        return step if step and step.get("unsure") and step.get("status") in lost else None

    @staticmethod
    def _stale_step(result):
        """The journey step that failed only because its checks describe the page before it, else None."""
        step = next((s for s in result.steps_judged if s["step"] == result.failed_step), None)
        return step if step and step.get("held_before") and not step.get("reload") else None

    def _before_typing(self, check):
        """The engine's before_execute: the write guard first (it may stop the action), then the value about to be
        typed is noted when the text model wrote it (typed_values.py)."""
        def before(action, page, text):
            check(action, page, text)
            self._typing.about_to_type(action, text)

        return before

    def _after_action(self, browser, flow):
        """The engine's after_action: wait for the page to settle, close a full-page ad the action brought up, then
        read the field of a value just typed."""
        heartbeat.beat()  # an action went through: the test is moving
        # after a scroll the page rarely loads anything: read it more often so a quiet page goes on sooner (a request
        # it starts, like lazy-loaded content, still holds the wait until it is answered)
        scrolled = (getattr(browser, "after_input", None) or {}).get("kind") == "scroll"
        try:
            self._wait_stable(browser, max_s=max(flow.settle_s, 1),
                              poll_s=min(self._poll_s, SCROLL_POLL_S) if scrolled else self._poll_s,
                              clock=self._clock, sleep=self._sleep)
            self._close_cover(browser, flow)
        finally:
            self._typing.typed(browser)

    def _close_cover(self, browser, flow=None, stuck=False):
        """A window from another site laid over the whole page (an ad between two pages) is closed the way a person
        closes it (interstitial.py), then the page it let through is waited for. Each one is remembered with its step,
        so a step that fails under one it couldn't close says so. -> True when one was closed."""
        from . import interstitial

        # after an ordinary action only an ad between pages is closed; once stuck, any window from another site
        answer = interstitial.close(browser, sleep=self._sleep, clock=self._clock, announced_only=not stuck)
        if not answer.get("found"):
            return False
        self.__dict__.setdefault("_covers", []).append({"step": getattr(self, "_step_number", 0),
                                                        "by": answer.get("by", ""),
                                                        "closed": answer.get("closed", False)})
        if answer.get("closed"):
            self._wait_stable(browser, max_s=max(flow.settle_s if flow else 0, 1), poll_s=self._poll_s,
                              clock=self._clock, sleep=self._sleep)
        return answer.get("closed", False)

    def _cover_at_end(self, agent):
        """A window from another site still over the page where the test ended: remembered as not closed."""
        from . import interstitial

        try:
            seen = interstitial.cover(agent.browser)
        except Exception:
            seen = None
        if seen:
            self.__dict__.setdefault("_covers", []).append({"step": getattr(self, "_step_number", 0), "by": seen["by"],
                                                            "closed": False})

    def _cover_left(self, result, flow):
        """The window from another site that covered the page where the test stopped and couldn't be closed, else
        None: one still over the page at the end, or one met at the step that failed."""
        at_step = result.failed_step if flow.steps else None
        return next((c for c in reversed(getattr(self, "_covers", []))
                     if not c["closed"] and (at_step is None or c["step"] == at_step)), None)

    def _recording(self, provider, secrets):
        """The engine's text provider, remembering what it typed (never a secret) for the "kept" check, and which
        model wrote it (typed_values.py)."""
        def typing(context, action, page):
            answer = provider(context, action, page)
            text = answer[0] if isinstance(answer, tuple) and answer else None
            self._typing.answered(text, answer[1] if isinstance(answer, tuple) and len(answer) > 1 else None)
            if isinstance(text, str) and text.strip() and not any(s and s in text for s in secrets):
                self._typed.append(text.strip())
            if isinstance(text, str) and text and text == getattr(self, "_password", None):
                self._password_typed = True  # a sign-in that went the password way: no email to wait for
            return answer

        return typing

    def _open_email(self, agent, flow, wait_s):
        """A journey's `open_email`: wait for the next email to its new account, open its action link in the same
        browser, and make its code typeable (email_code). -> None, or the Outcome saying what was missing."""
        message = mailbox.wait_for_new(self._inbox, self._emails_opened, wait_s=wait_s, clock=self._clock,
                                       sleep=self._sleep)
        if message is None:
            return assertions.Outcome("open_email", "an email to the new account", False,
                                      f"no email arrived within {wait_s} s")
        self._emails_opened.add(message["id"])
        self._by_email = True
        code = next(iter(message.get("codes") or []), "")
        chooser = getattr(self, "_chooser", None)
        if code and hasattr(chooser, "values"):
            chooser.values["email_code"] = code
            getattr(chooser, "_secret", set()).add("email_code")  # a one-time code is never shown to a model
        link = mailbox.action_link(message, [urlparse(o).hostname for o in getattr(self, "_origins", ())]
                                   or [urlparse(flow.start).hostname])
        if link:
            try:
                agent.browser.call("Page.navigate", url=link)
                self._wait_stable(agent.browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s,
                                  clock=self._clock, sleep=self._sleep)
                agent.state["page"] = agent.browser.observe(screenshot=agent.screenshots)
            except Exception:
                pass  # the checks read the page as it is: a link that didn't open shows there
        elif not code:
            return assertions.Outcome("open_email", "a link or a code in the email", False,
                                      f"“{message.get('subject', '')[:80]}” has neither")
        return None

    def _wait_for(self, agent, checks, wait_s, *, reload, flow):
        """A step's slow work: re-read the page every WAIT_POLL_S (reloading it first when the step reloads) until
        its checks hold or wait_s has passed. No model, no action; a picture each time for the replay. -> outcomes"""
        deadline, outcomes = self._clock() + wait_s, []
        while True:
            if reload:
                self._reload(agent, flow)
            outcomes = self._settled_outcomes(agent, checks, 1)
            if getattr(self, "_record_dir", None):
                self._final_frame(agent, self._record_dir)
            if (outcomes and all(o.passed for o in outcomes)) or self._clock() >= deadline:
                return outcomes
            self._sleep(min(WAIT_POLL_S, max(deadline - self._clock(), 0)))

    def _reload(self, agent, flow):
        """Reload the page, wait for it to settle, and let the agent see the reloaded page."""
        try:
            agent.browser.call("Page.reload", ignoreCache=False)
            self._wait_stable(agent.browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                              sleep=self._sleep)
            agent.state["page"] = agent.browser.observe(screenshot=agent.screenshots)
        except Exception:
            pass  # the checks below read the page as it is; a failed reload shows there

    @staticmethod
    def _step_picture(agent, output_dir, name, number):
        """The page a journey's step was judged on, as a picture (its proof). "" without an output folder."""
        if not output_dir:
            return ""
        try:
            data = agent.browser.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
        except Exception:
            return ""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / f"{name}.step{number}.jpg"
        path.write_bytes(base64.b64decode(data))
        return str(path)

    def _answer_consent(self, agent, result):
        """A cookie banner on the page is answered the most private way it offers before anything else (consent.py);
        the answer is one of the test's steps, so its replay and timeline show it."""
        from . import consent

        answer = consent.decline(agent.browser)
        said = consent.said(answer)
        if not said:
            return
        result.consent = said
        if answer.get("chose"):
            self._wait_stable(agent.browser, max_s=2, poll_s=self._poll_s, clock=self._clock, sleep=self._sleep)
            self._mark(agent, said)

    def _mark(self, agent, action):
        """One of the test's steps done by EveryRole itself (not the agent): in its steps, timeline and replay."""
        state = agent.state
        if "started_at" in state and state["started_at"] is None:
            state["started_at"] = time.perf_counter()
        at = round((time.perf_counter() - state["started_at"]) * 1000) if state.get("started_at") else 0
        history = state.setdefault("history", [])
        # the agent reads every step's fields (its stuck check: three steps in a row that changed nothing): a step
        # EveryRole did answered the page (a banner gone, a link followed), so it never counts as stuck
        history.append({"step": len(history) + 1, "action": action, "kind": "click", "text": None,
                        "page_changed": True, "url": self._url_of(agent), "executed_ms": at})

    @staticmethod
    def _url_of(agent):
        try:
            return agent.browser.evaluate("location.href") or ""
        except Exception:
            return ""

    def _click_link(self, agent, flow, guard, click, checks):
        """A way in's click done by EveryRole itself: the link that leads to `click["to"]` (its words told same-page
        links apart), scrolled into view and clicked where a person would. -> (status, outcomes), or None when no
        such link is on the page (a link inside a closed menu, a button that isn't a link): the agent does it then.
        Something fixed on screen over the link (a banner, a chat bubble) fails it, saying what covers it. A link
        whose words or address hold a never-click word ("/delete_account") is refused before anything is clicked."""
        from .site import split as split_page

        guard.check_action({"label": click.get("label", ""), "href": split_page(click.get("to", ""))[1],
                            "kind": "click"})
        if click.get("menu"):
            return None
        address = split_page(click["to"])[1].split("#", 1)[0]
        target, _, query = address.partition("?")
        target = target.rstrip("/") or "/"
        self._answer_consent_again(agent)
        try:
            found = agent.browser.evaluate(f"{CLICK_LINK}({json.dumps(target)}, {json.dumps(query)}, "
                                           f"{json.dumps(click.get('label', ''))})")
        except Exception:
            return None
        if not isinstance(found, dict) or not found.get("found"):
            return None
        if found.get("covered"):
            self._covered = {"step": self._step_number, "label": found.get("label") or click.get("label", ""),
                             "by": found.get("by", "")}
            return "failed_expect", [assertions.Outcome("click", click.get("label", ""), False,
                                                        f"covered by “{found.get('by', '')}”")]
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            agent.browser.call("Input.dispatchMouseEvent", type=kind, x=found["x"], y=found["y"],
                               button="left" if kind != "mouseMoved" else "none", clickCount=1)
        self._mark(agent, f"Clicked “{found.get('label') or click.get('label')}”")
        self._wait_stable(agent.browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                          sleep=self._sleep)
        self._close_cover(agent.browser, flow)
        outcomes = self._settle(agent, checks, flow.settle_s) if checks else []
        return ("passed" if all(o.passed for o in outcomes) else "failed_expect"), outcomes

    def _press_submit(self, agent, flow, submit, checks):
        """A form test's own button pressed by EveryRole: the button with those words inside a form (the one the agent
        filled first), scrolled into view and clicked where a person would. -> (status, outcomes), or None when no such
        button is on the page (the agent does it then). Something fixed over it fails the step, naming it."""
        self._answer_consent_again(agent)
        try:
            values = {k: v for k, v in (flow.values or {}).items() if isinstance(v, str)}
            found = agent.browser.evaluate(f"{PRESS_SUBMIT}({json.dumps(submit.get('label', ''))}, "
                                           f"{json.dumps(values)})")
        except Exception:
            return None
        if not isinstance(found, dict) or not found.get("found"):
            return None
        if found.get("covered"):
            self._covered = {"step": self._step_number, "label": found.get("label") or submit.get("label", ""),
                             "by": found.get("by", "")}
            return "failed_expect", [assertions.Outcome("click", submit.get("label", ""), False,
                                                        f"covered by “{found.get('by', '')}”")]
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            agent.browser.call("Input.dispatchMouseEvent", type=kind, x=found["x"], y=found["y"],
                               button="left" if kind != "mouseMoved" else "none", clickCount=1)
        if found.get("typed"):  # the fields of that form the agent left empty (a sibling form filled instead)
            self._mark(agent, "Filled in " + ", ".join(found["typed"]))
        self._mark(agent, f"Pressed “{found.get('label') or submit.get('label')}”")
        self._wait_stable(agent.browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                          sleep=self._sleep)
        self._close_cover(agent.browser, flow)
        outcomes = self._settle(agent, checks, flow.settle_s) if checks else []
        return ("passed" if all(o.passed for o in outcomes) else "failed_expect"), outcomes

    def _answer_consent_again(self, agent):
        """A banner that came back (a new page of another part of the app) is answered again before a click."""
        from . import consent

        if consent.decline(agent.browser).get("chose"):
            self._wait_stable(agent.browser, max_s=2, poll_s=self._poll_s, clock=self._clock, sleep=self._sleep)
        self._close_cover(agent.browser)

    # a check of what a page shows or where it is: when it already held before a step acted, it can't tell whether
    # the step did anything (other kinds, a save attempt or "nothing lost", are only ever met by acting)
    SAME_BEFORE_KINDS = frozenset({"text_visible", "element_visible", "url_contains", "url_matches"})

    def _facts_now(self, agent):
        """The page as it is now, read for checks; None when it can't be read."""
        try:
            return self._read(agent.browser, agent.browser.observe(screenshot=False))
        except Exception:
            return None

    def _discriminating(self, checks, facts):
        """-> (all held, the checks to judge the step on, the checks left out) for a step's checks read on the page
        before it acted. All held: none can tell whether the step worked (the caller fails it as the test's mistake).
        Some held: those prove nothing (a menu or footer label on every page), so the step is judged on the rest."""
        if facts is None or not checks:
            return False, checks, []
        before = assertions.evaluate(checks, facts)
        if all(o.passed for o in before):
            return True, checks, []
        same = [c.kind in self.SAME_BEFORE_KINDS and o.passed for c, o in zip(checks, before)]
        kept = [c for c, s in zip(checks, same) if not s]
        left_out = [{"kind": o.kind, "expected": o.expected} for o, s in zip(before, same) if s]
        return False, kept or checks, left_out if kept else []

    def _holds_now(self, agent, checks):
        """Every check already holds on the page as it is now (the start page, before the first step)."""
        try:
            outcomes = assertions.evaluate(checks, self._read(agent.browser, agent.browser.observe(screenshot=False)))
        except Exception:
            return False
        return bool(outcomes) and all(o.passed for o in outcomes)

    def _conclude(self, agent, flow, checks):
        outcomes = []
        final = agent.snapshot()
        if final["status"] == "blocked" and len(final["history"]) >= flow.max_steps and not flow.expect_denied:
            return "budget_exceeded", outcomes
        # The agent stopped; the page may still be redirecting or rendering. Re-check for settle_s.
        outcomes = self._settle(agent, checks, flow.settle_s) or outcomes
        if outcomes and all(o.passed for o in outcomes):
            return "passed", outcomes
        return ("blocked" if final["status"] == "blocked" else "failed_expect"), outcomes

    def _settled_outcomes(self, agent, checks, settle_s):
        """Wait for the page to finish loading (up to settle_s), then evaluate the checks on it."""
        self._wait_stable(agent.browser, max_s=settle_s, poll_s=self._poll_s, clock=self._clock, sleep=self._sleep)
        try:
            page = agent.browser.observe(screenshot=False)
            return assertions.evaluate(checks, self._read(agent.browser, page))
        except Exception:
            return []

    def _settle(self, agent, checks, settle_s):
        deadline = self._clock() + settle_s
        outcomes = []
        while True:
            try:
                page = agent.browser.observe(screenshot=False)
                outcomes = assertions.evaluate(checks, self._read(agent.browser, page))
            except Exception:
                pass  # mid-navigation reads can fail; keep polling until the deadline
            if (outcomes and all(o.passed for o in outcomes)) or self._clock() >= deadline:
                return outcomes
            self._sleep(self._poll_s)

    def _final_outcomes(self, agent, checks, outcomes):
        """Re-check on a fresh read so the reported state is the page as it is now."""
        try:
            page = agent.browser.observe(screenshot=False)
            return assertions.evaluate(checks, self._read(agent.browser, page))
        except Exception:
            return outcomes

    @staticmethod
    def _final_frame(agent, record_dir):
        """The page the checks were judged on, as the replay's last frame."""
        started = agent.snapshot().get("started_at")
        existing = [int(p.stem) for p in Path(record_dir).glob("*.jpg") if p.stem.isdigit()]
        at = round((time.perf_counter() - started) * 1000) if started else max(existing, default=0) + 500
        try:
            data = agent.browser.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
        except Exception:
            return
        Path(record_dir).mkdir(parents=True, exist_ok=True)
        (Path(record_dir) / f"{max(at, max(existing, default=0) + 1):06d}.jpg").write_bytes(base64.b64decode(data))

    @staticmethod
    def _make_video(result, record_dir):
        from . import replay_cursor
        from .video import make_video, on_replay, timeline

        record_dir = Path(record_dir)
        taken = [p for p in record_dir.glob("*.jpg") if p.stem.isdigit()] if record_dir.is_dir() else []
        if taken:  # the page the checks were judged on, kept as a picture (without the cursor) before encoding
            poster = record_dir.with_suffix(".jpg")
            poster.write_bytes(max(taken, key=lambda p: int(p.stem)).read_bytes())
            result.poster = str(poster)
        replay_cursor.draw(record_dir, result.steps)  # adds frames: read the frames' times after it
        # Read the final frame's time before encoding: encoding removes the frames.
        existing = [int(p.stem) for p in record_dir.glob("*.jpg") if p.stem.isdigit()] if record_dir.is_dir() else []
        last_step = max((s.get("executed_ms") or 0 for s in result.steps), default=0)
        marks = timeline(result.steps, max([last_step, *existing]) or result.duration_ms, judged=result.steps_judged)
        result.timeline = [{**mark, "ms": on_replay(mark["ms"], existing)} for mark in marks]  # idle cut short
        video = make_video(record_dir, record_dir.with_suffix(".mp4"))
        if video:
            result.video = video

    def _read(self, browser, page):
        """Page facts, without the flow's known background writes (analytics/metrics are not save attempts)."""
        facts = self._facts(browser, page)
        if hasattr(self, "_seen_paths"):
            self._seen_paths.add(urlparse(facts.url or "").path)
        self._passwords_seen = max(getattr(self, "_passwords_seen", 0), facts.passwords)
        if facts.passwords and getattr(self, "_form_words", None) == []:
            self._form_words = form_words(browser) or None  # read once: what the form said before anything was sent
        ignore = getattr(self, "_ignore_saves", ())
        if ignore and facts.blocked:
            kept = tuple(b for b in facts.blocked if not any(fragment in b[1] for fragment in ignore))
            facts = replace(facts, blocked=kept)
        dialogs = self._page_dialogs(browser)
        start = getattr(self, "_dialogs_from", 0)
        if dialogs:  # a new page starts its own list: then all of them are this step's
            facts = replace(facts, dialogs=tuple(dialogs[start:] if len(dialogs) >= start else dialogs))
        if getattr(self, "_typed_before", ()) or getattr(self, "_inbox", ""):
            facts = replace(facts, **self._typed_facts(facts.url), emails=tuple(
                m.get("subject", "") + "\n" + m.get("text", "") for m in mailbox.messages(self._inbox)))
        self._last_facts = facts
        return facts

    @staticmethod
    def _page_dialogs(browser):
        """The messages of the browser dialogs the page showed (the engine answers them, never blocking, and keeps
        each in window.__jevDialogs), oldest first; [] when they can't be read."""
        try:
            seen = browser.evaluate("(window.__jevDialogs || []).map(d => String(d.message || ''))")
        except Exception:
            return []
        return [str(m) for m in seen] if isinstance(seen, list) else []

    def _typed_facts(self, url):
        """What the "kept" check judges on this page. Only where the visitor is back at their work: a page the journey
        was on before this step. A page new to it (an onboarding, a "check your email") waits for a later step
        (a sign-up step ended on the onboarding, which never shows the website address typed
        before it). The last step judges it wherever the journey ends."""
        here = urlparse(url or "").path
        elsewhere = bool(self._typed_before) and not self._last_step and here not in self._seen_before
        return {"typed": self._typed_before, "kept_elsewhere": elsewhere}

    def _signed_out(self, agent, signed_out):
        try:
            return signed_out(self._read(agent.browser, agent.browser.observe(screenshot=False))) or ""
        except Exception:
            return ""  # the page can't be read: nothing says the session expired

    def _save_session(self, agent, result, path):
        try:
            save(capture(agent.browser, getattr(self, "_origins", ())), path)
            result.session_saved = True
        except Exception as exc:
            result.passed, result.status = False, "error"
            result.error = f"signed in, but the session could not be saved: {type(exc).__name__}: {exc}"

    def _finish(self, agent, result, outcomes, output_dir, flow):
        snapshot = agent.snapshot()
        all_passed = bool(outcomes) and all(o.passed for o in outcomes)
        # Denial flows: the agent giving up or running out of steps is the expected outcome.
        recoverable = {"error", "failed_expect", "blocked"} | ({"budget_exceeded"} if flow.expect_denied else set())
        if result.status in recoverable and all_passed and not self._journey_open:
            # The page satisfies every expectation even though the agent errored or gave up.
            if result.error:
                result.warnings.append(f"agent error after success: {result.error}")
            result.status, result.error = "passed", ""
        elif result.status == "passed" and not all_passed:
            result.status = "failed_expect"
            result.warnings.append("expectations passed during the run but not on the final page")
        result.passed = result.status == "passed"
        if not result.passed:  # only a person can pass a security check: say so instead of blaming the app
            from .challenge import detect

            result.challenge = detect(agent.browser)
            result.server_answer = server_answer(agent.browser)  # a server that didn't answer: nothing to judge
        result.expectations = [o.as_dict() for o in outcomes]
        result.steps = [{k: h.get(k) for k in STEP_KEYS} for h in snapshot["history"]]
        self._typing.settle(agent.browser)
        result.typed_values = self._typing.records()
        result.decisions = len(snapshot["decisions"])
        text_calls = snapshot.get("text_calls") or []
        by_slot = {role: [c for c in text_calls if answered_slot(c) == role] for role in MODEL_ROLES}
        result.usage = {"engine": usage.of_calls(snapshot["decisions"]),
                        **{usage_group("text", role): usage.of_calls(calls) for role, calls in by_slot.items()}}
        result.input_tokens = sum(d.get("usage", {}).get("input_tokens", 0) for d in snapshot["decisions"]) + sum(
            c.get("usage", {}).get("input_tokens", 0) for c in snapshot.get("text_calls", [])
        )
        result.final_url = snapshot["page"].get("url", "")
        facts = getattr(self, "_last_facts", None)
        if not result.passed and facts is not None:
            result.page_text = page_excerpt(facts.text)
            result.blocked_saves = [f"{method} {url}" for method, url in facts.blocked if method != "BEACON"]
        if not result.passed and output_dir:
            result.screenshot = self._screenshot(agent, Path(output_dir), result)

    def _screenshot(self, agent, output_dir, result):
        try:
            data = agent.browser.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
        except Exception as exc:
            result.warnings.append(f"screenshot failed: {type(exc).__name__}: {exc}")
            return ""
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{result.name}.jpg"
        path.write_bytes(base64.b64decode(data))
        return str(path)

    @staticmethod
    def _redacted(result, redactor):
        return FlowResult(**redactor.scrub(result.as_dict()))


def _stale_note(agent):
    """Why a budget ran out without progress: decisions dropped because the page changed before acting."""
    stale = (agent.snapshot().get("stale") if agent is not None else None) or []
    if not stale:
        return ""
    reasons = {}
    for item in stale:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    top = ", ".join(f"{reason} ({n}x)" for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:2])
    return f"; {len(stale)} decisions dropped because the page changed first: {top}"
