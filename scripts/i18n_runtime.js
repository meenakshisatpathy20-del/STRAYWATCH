// Runtime: pick the user's language (chosen in the chat) and fill every [data-i18n] element.
function swLang() { try { return localStorage.getItem("sw_lang_code") || "en"; } catch (e) { return "en"; } }
function t(key) { const l = I18N[swLang()] || I18N.en; return (l && l[key]) || I18N.en[key] || key; }
function applyI18n(root) {
  (root || document).querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  (root || document).querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  (root || document).querySelectorAll("[data-i18n-label]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nLabel)); el.title = t(el.dataset.i18nLabel); });
  document.documentElement.lang = swLang();
}
window.swT = t;
window.applyI18n = applyI18n;
document.addEventListener("DOMContentLoaded", () => applyI18n());
window.addEventListener("sw-lang", () => applyI18n());
