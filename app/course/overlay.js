// Нумерация интерактивных элементов: [N] на скриншоте = [N] в списке элементов = aria-label «[N] …»
// в снапшоте Playwright MCP. Номера рисует код, не модель. Вызов: page.evaluate(OVERLAY, submitClasses).
(submitClasses) => {
  const SELECTOR = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=tab]', '[contenteditable=true]',
  ].join(', ');
  const BOX = '__bitowi_overlay';

  // атрибут меняем, исходное значение — в data-атрибут; при следующем осмотре возвращаем
  const stash = (el, attr, keep, value) => {
    el.setAttribute(keep, el.getAttribute(attr) || '');
    if (value === null) el.removeAttribute(attr); else el.setAttribute(attr, value);
  };
  const restore = (el, attr, keep) => {
    if (!el.hasAttribute(keep)) return;
    const value = el.getAttribute(keep);
    if (value) el.setAttribute(attr, value); else el.removeAttribute(attr);
    el.removeAttribute(keep);
  };

  // прошлая разметка: вернуть исходные aria-label, aria-labelledby, role, aria-checked
  document.getElementById(BOX)?.remove();
  document.querySelectorAll('[data-bitowi-n]').forEach((el) => {
    restore(el, 'aria-label', 'data-bitowi-aria');
    restore(el, 'aria-labelledby', 'data-bitowi-labelledby');
    restore(el, 'role', 'data-bitowi-role');
    restore(el, 'aria-checked', 'data-bitowi-checked');
    el.removeAttribute('data-bitowi-n');
  });

  const squash = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };

  const kindOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role');
    if (tag === 'a' || role === 'link') return 'link';
    if (tag === 'select') return 'select';
    if (tag === 'textarea' || el.isContentEditable) return 'textarea';
    if (tag === 'input') {
      if (type === 'checkbox' || type === 'radio' || type === 'file') return type;
      if (['submit', 'button', 'image', 'reset'].includes(type)) return 'button';
      return 'text';
    }
    if (role === 'checkbox' || role === 'radio') return role;
    if (tag === 'button' || tag === 'summary' || role === 'button' || role === 'tab') return 'button';
    return 'other';
  };

  // Moodle: у radio aria-labelledby на div с текстом варианта
  const labelledBy = (el) => (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map((id) => document.getElementById(id)).filter(Boolean);

  // скрытый radio/checkbox (оформленный переключатель): кликают по его подписи
  const proxyFor = (input) => {
    const candidates = [...(input.labels || []), input.closest('label'), ...labelledBy(input)];
    return candidates.find((c) => c && visible(c) && !c.hasAttribute('data-bitowi-n')) || null;
  };

  const labelOf = (el, kind) => {
    const parts = [el.getAttribute('aria-label'), ...labelledBy(el).map((l) => l.innerText)];
    if (el.labels && el.labels.length) parts.push([...el.labels].map((l) => l.innerText).join(' '));
    else if (el.closest('label')) parts.push(el.closest('label').innerText);
    if (!['text', 'textarea', 'select'].includes(kind)) parts.push(el.innerText);
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type)) parts.push(el.value);
    parts.push(el.getAttribute('placeholder'), el.title, el.getAttribute('alt'));
    const seen = [];
    for (const p of parts.map(squash)) if (p && !seen.some((s) => s.includes(p))) seen.push(p);
    return seen.join(' · ').slice(0, 200);
  };

  // текст страницы — до бейджей, чтобы в нём не было «[1][2]…»
  const text = document.body ? document.body.innerText : '';
  const messages = [...document.querySelectorAll('[role=alert], [role=status], .alert, .feedback, .notification')]
    .filter(visible).map((el) => squash(el.innerText)).filter(Boolean);
  const bar = document.querySelector('[role=progressbar]');
  const now = bar && bar.getAttribute('aria-valuenow');
  const progress = bar ? squash(bar.getAttribute('aria-valuetext') || (now ? now + '%' : bar.innerText)) : '';

  const box = document.createElement('div');
  box.id = BOX;
  box.setAttribute('aria-hidden', 'true');
  box.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483647';

  const elements = [];
  let n = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (el.closest('#' + BOX)) continue;
    const kind = kindOf(el);
    // target — что нумеруем и по чему кликают: сам элемент или подпись скрытого переключателя
    let target = el;
    if (!visible(el)) {
      if (kind !== 'radio' && kind !== 'checkbox') continue;
      target = proxyFor(el);
      if (!target) continue;
    }
    if (target.hasAttribute('data-bitowi-n')) continue;
    // ссылка внутри кнопки и т.п. — нумеруем внешний элемент
    if (target.parentElement && target.parentElement.closest('[data-bitowi-n]')) continue;
    n += 1;
    const label = labelOf(el, kind);
    target.setAttribute('data-bitowi-n', String(n));
    // номер — в доступном имени; aria-labelledby важнее aria-label, поэтому его убираем на время
    stash(target, 'aria-label', 'data-bitowi-aria', `[${n}] ${label}`.trim());
    if (target.hasAttribute('aria-labelledby')) stash(target, 'aria-labelledby', 'data-bitowi-labelledby', null);
    if (target !== el) {
      stash(target, 'role', 'data-bitowi-role', kind);
      stash(target, 'aria-checked', 'data-bitowi-checked', String(!!el.checked));
    }

    let value = '';
    let options = [];
    if (kind === 'text' || (kind === 'textarea' && 'value' in el)) value = el.value || '';
    else if (kind === 'textarea') value = el.innerText || '';
    else if (kind === 'select') {
      options = [...el.options].map((o) => squash(o.text));
      value = el.selectedIndex >= 0 ? squash(el.options[el.selectedIndex].text) : '';
    }
    let checked = null;
    if (kind === 'checkbox' || kind === 'radio') {
      checked = 'checked' in el ? el.checked : el.getAttribute('aria-checked') === 'true';
    }
    elements.push({
      n, kind, label, value: value.slice(0, 500), options, checked,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      href: el.href && typeof el.href === 'string' ? el.href : '',
      submit_class: !!(el.matches && el.matches(submitClasses)),
    });

    // бейдж — canvas: нарисованный номер не попадает ни в дерево доступности (снапшот MCP), ни в innerText
    const r = target.getBoundingClientRect();
    const label_ = `[${n}]`;
    const badge = document.createElement('canvas');
    const w = 8 * label_.length + 6;
    badge.width = w;
    badge.height = 16;
    badge.dataset.n = String(n);
    badge.style.cssText =
      `position:absolute;left:${Math.max(0, r.left + scrollX - 4)}px;top:${Math.max(0, r.top + scrollY - 12)}px`;
    const g = badge.getContext('2d');
    g.fillStyle = '#ffd400';
    g.fillRect(0, 0, w, 16);
    g.strokeStyle = '#000';
    g.strokeRect(0.5, 0.5, w - 1, 15);
    g.fillStyle = '#000';
    g.font = 'bold 12px monospace';
    g.textBaseline = 'middle';
    g.fillText(label_, 3, 8.5);
    box.appendChild(badge);
  }
  document.body.appendChild(box);

  // метка разметки: если страница сменилась, номера устарели
  window.__bitowiStamp = Math.random().toString(36).slice(2);
  return {
    stamp: window.__bitowiStamp,
    url: location.href,
    title: document.title,
    text, messages, progress, elements,
    height: document.documentElement.scrollHeight,
    viewport: innerHeight,
  };
}
