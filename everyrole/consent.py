"""Cookie and consent banners: answered the most private way before EveryRole acts on a page, never "Accept all".

A consent banner sits fixed over the page (often its bottom, where the footer links are) until someone answers it, so
clicks meant for the page land on the banner. Before exploring a page and before a test acts, EveryRole finds the
banner the way a person would see it, and answers it the most private way the banner offers:
1. a button that refuses or keeps only the required cookies ("Reject all", "Only required", "Necessary only"...);
2. else the banner's switches turned off, then its "save choices" button;
3. else nothing: a banner that only offers to accept is left as it is (EveryRole never accepts for anyone).

What makes a banner, without anything written for one site: a consent-management tool's own container (the widely used
ones mark theirs: OneTrust, Cookiebot, Didomi, Usercentrics, Quantcast, TrustArc, Osano, Complianz, CookieYes,
iubenda, Klaro, Termly, Axeptio, Borlabs, cookieconsent), or any visible box fixed or sticky on screen (or an open
dialog) whose words are about cookies or consent and that holds at least two buttons. Buttons are read by their
labels, in the languages the vocabulary below covers.

decline(browser) -> {"found": bool, "chose": label or "", "how": "button" | "switches" | ""}.
"""

import json

# the containers consent-management tools give their banners (their documented ids and classes)
CMP_SELECTORS = (
    "#onetrust-banner-sdk", "#onetrust-consent-sdk", "#CybotCookiebotDialog", "#didomi-host", "#didomi-popup",
    "#usercentrics-root", "#uc-banner", ".qc-cmp2-container", "#qc-cmp2-ui", "#truste-consent-track",
    ".truste_box_overlay", ".osano-cm-window", ".osano-cm-dialog", ".cmplz-cookiebanner", ".cky-consent-container",
    "#cky-consent", "#iubenda-cs-banner", ".klaro", "#termly-code-snippet-support", ".axeptio_widget",
    "#BorlabsCookieBox", ".cc-window", ".cc-banner", "#cookie-law-info-bar", "#cookiescript_injected",
)
# what a banner's words are about (a box must speak of this to count, so no other dialog is ever answered)
# (never "privacy" alone: menus and footers say it)
ABOUT = r"cookie|consent|gdpr|rgpd|dsgvo|traceurs|galleta|biscotti|consentement|einwilligung|consenso"
# the most private answer: refuse, or keep only what is required
REFUSE = (r"reject|decline|deny|refuse|disagree|no,? thanks|do not (accept|agree)|"
          r"(only|just) (the )?(required|necessary|essential|strictly necessary)|"
          r"(required|necessary|essential|strictly necessary)( cookies)? only|"
          r"use (only )?(necessary|essential|required)|continue without (accepting|agreeing)|"
          r"tout refuser|refuser|continuer sans accepter|seulement (les )?(n[eé]cessaires|essentiels)|"
          r"ablehnen|nur (notwendige|erforderliche|essenzielle)|rechazar|solo (necesarias|esenciales)|"
          r"rifiuta|solo (necessari|essenziali)|recusar|rejeitar|weigeren|alleen (noodzakelijke|essenti)")
# never pressed: anything that agrees
ACCEPT = (r"accept|agree|allow|okay|\bok\b|got it|i understand|consent to|enable all|alles? akzeptieren|"
          r"tout accepter|accepter|j'accepte|akzeptieren|zustimmen|aceptar|accetta|aceitar|accepteren|toestaan")
# saving one's own choices
SAVE = (r"save|confirm|submit|apply|done|enregistrer|valider|confirmer|speichern|best[aä]tigen|guardar|confirmar|"
        r"salva|conferma|opslaan|bevestigen")

DECLINE_JS = r"""((cmp, about, refuse, accept, save) => {
  const ABOUT = new RegExp(about, 'i'), REFUSE = new RegExp(refuse, 'i'), ACCEPT = new RegExp(accept, 'i'),
        SAVE = new RegExp(save, 'i');
  const shown = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0; };
  const fixed = el => { for (let e = el; e && e !== document.body; e = e.parentElement) {
    const p = getComputedStyle(e).position; if (p === 'fixed' || p === 'sticky') return true; } return false; };
  const words = el => (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const kinds = 'button, [role="button"], a, input[type="button"], input[type="submit"]';
  const buttons = box => [...box.querySelectorAll(kinds)]
    .filter(b => shown(b) && words(b) && words(b).length <= 60);
  const boxes = [];
  for (const sel of cmp) for (const el of document.querySelectorAll(sel)) if (shown(el)) boxes.push(el);
  for (const el of document.querySelectorAll('body *')) {
    if (boxes.some(b => b.contains(el) || el.contains(b))) continue;
    const s = getComputedStyle(el);
    const dialog = el.matches('dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]');
    if (!(dialog || s.position === 'fixed' || s.position === 'sticky') || !shown(el)) continue;
    const text = words(el);
    if (text.length > 1500 || !ABOUT.test(text) || buttons(el).length < 2) continue;
    boxes.push(el);
  }
  for (const box of boxes) {
    const all = buttons(box);
    if (!all.length) continue;
    const no = all.find(b => REFUSE.test(words(b)));
    if (no) { no.click(); return {found: true, chose: words(no), how: 'button'}; }
    const on = [...box.querySelectorAll('input[type="checkbox"], [role="switch"], [role="checkbox"]')].filter(i =>
      shown(i.closest('label') || i) && !i.disabled && i.getAttribute('aria-disabled') !== 'true' &&
      (i.checked === true || i.getAttribute('aria-checked') === 'true'));
    const keep = all.find(b => SAVE.test(words(b)) && !ACCEPT.test(words(b)));
    if (keep) { on.forEach(i => i.click()); keep.click(); return {found: true, chose: words(keep), how: 'switches'}; }
    return {found: true, chose: '', how: '', buttons: all.map(words)};
  }
  return {found: false, chose: '', how: ''};
})"""


def script():
    """The banner-answering script, ready to evaluate in a page."""
    args = ", ".join(json.dumps(a) for a in (list(CMP_SELECTORS), ABOUT, REFUSE, ACCEPT, SAVE))
    return f"{DECLINE_JS}({args})"


def decline(browser):
    """Answer a consent banner on the page the most private way it offers. -> {"found", "chose", "how"}."""
    try:
        answer = browser.evaluate(script())
    except Exception:
        return {"found": False, "chose": "", "how": ""}
    return answer if isinstance(answer, dict) else {"found": False, "chose": "", "how": ""}


def said(answer):
    """The answer in plain words for a test's steps and replay, or "" when there was no banner."""
    if not answer or not answer.get("found"):
        return ""
    if answer.get("how") == "button":
        return f"Cookie banner: chose “{answer['chose']}”"
    if answer.get("how") == "switches":
        return f"Cookie banner: turned off the optional cookies, then “{answer['chose']}”"
    return "Cookie banner: it only offers to accept, so it was left open"
