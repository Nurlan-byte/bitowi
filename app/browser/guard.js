// Защита страницы. Регулярки и режим подставляет app/browser/guard.py (один источник с кодом).
//   extract — шаг извлечения: блокирует кнопки сдачи, проверки, «Показать ответ» и сброс;
//   course  — режим курса: ещё чёрный список (выход, профиль, удаление, форум…), локальные адреса,
//             file:, скачивание; сдача — только по одноразовому разрешению оркестратора в sessionStorage.
//             На чужом домене (не сайт курса) по разрешению — любая отправка формы. Остальные сайты открыты:
//             сетевую политику держат маршрут и прокси браузера курса.
(() => {
  const POLICY = __BITOWI_POLICY__;
  if (window.__bitowiGuard) return;
  window.__bitowiGuard = true;

  const re = (src) => new RegExp(src, 'i');
  const SUBMIT = re(POLICY.submit);
  const NAV = re(POLICY.nav);
  const STATUS = new RegExp(POLICY.status, 'gi');
  const CONSENT = re(POLICY.consent);
  const CONSENT_BOX = re(POLICY.consent_box);
  const DANGER = re(POLICY.danger);
  const DENY = re(POLICY.deny);
  const DENY_HREF = re(POLICY.deny_href);
  const COURSE = POLICY.mode === 'course';
  const CLICKABLE = 'button, input[type=submit], input[type=button], input[type=image], a, [role=button]';

  const label = (el) =>
    // name не берём: у Moodle «Завершить попытку» name="next"
    [el.innerText, el.value, el.getAttribute && el.getAttribute('aria-label'), el.title]
      .filter(Boolean).join(' ').trim();

  // переход по ссылке на другую страницу или пункт меню — не сдача (та же функция — в overlay.js).
  // Ссылка-кнопка на JS (href="#…", javascript:) проверяется по тексту, как кнопка
  const MENU_ROLES = ['menuitem', 'menuitemradio', 'menuitemcheckbox', 'tab', 'treeitem'];
  const navItem = (el) => {
    if (MENU_ROLES.includes(el.getAttribute && el.getAttribute('role'))) return true;
    if (el.tagName !== 'A' || !el.hasAttribute('href') || el.getAttribute('href').trim().startsWith('#')) return false;
    try {
      const u = new URL(el.href, location.href);
      return /^https?:$/.test(u.protocol) && !u.hash;
    } catch (e) {
      return false;
    }
  };

  // то же правило, что guard.is_submit в Python
  const isSubmit = (el) => {
    if (!el) return false;
    const text = label(el).replace(STATUS, ' ');
    if (DANGER.test(text)) return true;
    if (navItem(el)) return false;
    if (el.matches && el.matches(POLICY.submit_classes)) return !NAV.test(text) || SUBMIT.test(text);
    return SUBMIT.test(text) && !NAV.test(text);
  };

  // согласие («I understand», «Согласен», флажок honor code) — то же правило, что guard.is_consent в Python.
  // Флажок находим и по клику на его подпись
  const BOXES = 'input[type=checkbox], [role=checkbox]';
  const boxOf = (t) => {
    const direct = t.closest(BOXES);
    if (direct) return direct;
    const lab = t.closest('label');
    return lab ? lab.control || lab.querySelector(BOXES) : null;
  };
  const boxText = (box) => {
    const ids = (box.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    return [box.getAttribute('aria-label'), ...[...(box.labels || [])].map((l) => l.innerText),
      box.closest('label') && box.closest('label').innerText,
      ...ids.map((id) => document.getElementById(id)).filter(Boolean).map((l) => l.innerText)]
      .filter(Boolean).join(' ');
  };
  const consentOf = (t) => {
    if (!COURSE || !t || !t.closest) return null;
    const box = boxOf(t);
    if (box) return CONSENT_BOX.test(boxText(box)) ? box : null;
    const el = t.closest(CLICKABLE);
    return el && CONSENT.test(label(el)) ? el : null;
  };

  const hrefOf = (el) => {
    const a = el && el.closest ? el.closest('a[href]') : null;
    return a ? a.href : '';
  };

  // локальный адрес по записи (имя, IP); после DNS то же проверяет прокси
  const PRIVATE_V4 = /^(127\.|10\.|0\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)/;
  const PRIVATE_V6 = /^\[(::1?|::ffff:|f[cd][0-9a-f]{0,2}:|fe[89ab][0-9a-f]?:)/i;
  const BAD_SCHEME = /^(file|chrome|chrome-extension|devtools|view-source|chrome-search):/i;
  const forbiddenUrl = (href) => {
    if (BAD_SCHEME.test(href)) return 'scheme';
    try {
      const u = new URL(href, location.href);
      if (!/^(https?|wss?):$/.test(u.protocol)) return '';
      const port = u.port || ({ 'https:': '443', 'wss:': '443' }[u.protocol] || '80');
      if ((POLICY.exempt || []).includes(`${u.hostname.toLowerCase()}:${port}`)) return '';
      const h = u.hostname.toLowerCase();
      if (h === 'localhost' || h.endsWith('.localhost') || PRIVATE_V4.test(h) || PRIVATE_V6.test(h)) return 'private';
    } catch (e) {
      return '';
    }
    return '';
  };

  // страница не на сайте курса — там по разрешению любая отправка формы
  const site = (POLICY.course_site || '').toLowerCase();
  const external = () => {
    const h = location.hostname.toLowerCase();
    return COURSE && site && !(h === site || h.endsWith('.' + site));
  };
  const formSubmitter = (el) =>
    el && el.form && (el.matches('input[type=submit], input[type=image]') ||
      (el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit').toLowerCase() === 'submit'));

  // причина запрета или '' — только в режиме курса
  const denied = (el) => {
    if (!COURSE || !el) return '';
    if (DENY.test(label(el))) return 'deny';
    const href = hrefOf(el);
    if (href && DENY_HREF.test(href)) return 'deny';
    if (href && forbiddenUrl(href)) return forbiddenUrl(href);
    if (el.closest && el.closest('a[download]')) return 'download';
    return '';
  };

  // одноразовое разрешение сдачи: {count, until}; sessionStorage переживает переход на страницу подтверждения
  const takeAllowance = () => {
    if (!COURSE) return false;
    try {
      const a = JSON.parse(sessionStorage.getItem(POLICY.allow_key) || 'null');
      if (!a || a.count < 1 || Date.now() > a.until) return false;
      a.count -= 1;
      sessionStorage.setItem(POLICY.allow_key, JSON.stringify(a));
      return true;
    } catch (e) {
      return false;
    }
  };

  const block = (event, el, why) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (el && el.setAttribute) el.setAttribute('data-bitowi-blocked', why);
    console.warn('[bitowi-guard] заблокировано (' + why + '): ' + (el && el.nodeType === 1 ? label(el) : ''));
  };

  document.addEventListener('click', (event) => {
    const el = event.target && event.target.closest ? event.target.closest(CLICKABLE) : null;
    const why = el ? denied(el) : '';
    if (why) return block(event, el, why);
    // согласие — по одноразовому разрешению после «Нажать» владельца
    const consent = consentOf(event.target);
    if (consent) {
      // клик по подписи порождает второй клик — по самому флажку: он идёт по тому же разрешению
      if (consent.__bitowiAllowed) return;
      if (takeAllowance()) {
        consent.__bitowiAllowed = true;
        setTimeout(() => { consent.__bitowiAllowed = false; }, 0);
        return;
      }
      return block(event, event.target.closest('[data-bitowi-n]') || consent, 'consent');
    }
    if (!el) return;
    if (isSubmit(el) || (external() && formSubmitter(el))) {
      if (!DANGER.test(label(el)) && takeAllowance()) {
        el.__bitowiAllowed = true;  // submit этой же кнопки не тратит второе разрешение
        return;
      }
      block(event, el, 'submit');
    }
  }, true);

  document.addEventListener('submit', (event) => {
    const sub = event.submitter;
    if (sub && sub.__bitowiAllowed) {
      sub.__bitowiAllowed = false;
      return;
    }
    // на сайте курса формы навигации (Далее, Начать попытку) свободны; на чужом домене — только по разрешению
    if (!external() && sub && !isSubmit(sub) && !denied(sub)) return;
    if (!sub && takeAllowance()) return;
    block(event, sub || event.target, sub ? 'submit' : 'form');
  }, true);

  // form.submit() не порождает событие submit
  const nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    if (takeAllowance()) return nativeSubmit.call(this);
    console.warn('[bitowi-guard] заблокировано: form.submit()');
  };

  if (COURSE) {
    const nativeOpen = window.open;
    window.open = function (url, ...rest) {
      if (url && forbiddenUrl(String(url))) {
        console.warn('[bitowi-guard] заблокировано (private): window.open');
        return null;
      }
      return nativeOpen.call(window, url, ...rest);
    };
  }
})();
