// Защита страницы. Регулярки и режим подставляет app/browser/guard.py (один источник с кодом).
//   extract — шаг извлечения: блокирует кнопки сдачи, проверки, «Показать ответ» и сброс;
//   course  — режим курса: ещё чёрный список (выход, профиль, удаление, форум…) и ссылки на чужой
//             домен; сдача — только по одноразовому разрешению оркестратора в sessionStorage.
(() => {
  const POLICY = __BITOWI_POLICY__;
  if (window.__bitowiGuard) return;
  window.__bitowiGuard = true;

  const re = (src) => new RegExp(src, 'i');
  const SUBMIT = re(POLICY.submit);
  const NAV = re(POLICY.nav);
  const DANGER = re(POLICY.danger);
  const DENY = re(POLICY.deny);
  const DENY_HREF = re(POLICY.deny_href);
  const COURSE = POLICY.mode === 'course';
  const CLICKABLE = 'button, input[type=submit], input[type=button], input[type=image], a, [role=button]';

  const label = (el) =>
    // name не берём: у Moodle «Завершить попытку» name="next"
    [el.innerText, el.value, el.getAttribute && el.getAttribute('aria-label'), el.title]
      .filter(Boolean).join(' ').trim();

  // то же правило, что guard.is_submit в Python
  const isSubmit = (el) => {
    if (!el) return false;
    const text = label(el);
    if (DANGER.test(text)) return true;
    if (el.matches && el.matches(POLICY.submit_classes)) return !NAV.test(text) || SUBMIT.test(text);
    return SUBMIT.test(text) && !NAV.test(text);
  };

  const hrefOf = (el) => {
    const a = el && el.closest ? el.closest('a[href]') : null;
    return a ? a.href : '';
  };

  const offsite = (href) => {
    try {
      const u = new URL(href, location.href);
      return /^https?:$/.test(u.protocol) && u.origin !== location.origin;
    } catch (e) {
      return false;
    }
  };

  // причина запрета или '' — только в режиме курса
  const denied = (el) => {
    if (!COURSE || !el) return '';
    if (DENY.test(label(el))) return 'deny';
    const href = hrefOf(el);
    if (href && DENY_HREF.test(href)) return 'deny';
    if (href && offsite(href)) return 'offsite';
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
    if (!el) return;
    const why = denied(el);
    if (why) return block(event, el, why);
    if (isSubmit(el)) {
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
    if (sub && !isSubmit(sub) && !denied(sub)) return;
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
      if (url && offsite(String(url))) {
        console.warn('[bitowi-guard] заблокировано (offsite): window.open');
        return null;
      }
      return nativeOpen.call(window, url, ...rest);
    };
  }
})();
