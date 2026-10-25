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
      if (type === 'checkbox' || type === 'radio' || type === 'file' || type === 'password') return type;
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

  // переход по ссылке на другую страницу или пункт меню — не сдача (та же функция — в guard.js)
  const MENU_ROLES = ['menuitem', 'menuitemradio', 'menuitemcheckbox', 'tab', 'treeitem'];
  const navItem = (el) => {
    if (MENU_ROLES.includes(el.getAttribute('role'))) return true;
    if (el.tagName !== 'A' || !el.hasAttribute('href') || el.getAttribute('href').trim().startsWith('#')) return false;
    try {
      const u = new URL(el.href, location.href);
      return /^https?:$/.test(u.protocol) && !u.hash;
    } catch (e) {
      return false;
    }
  };

  const chosen = new Set();
  // скрытый radio/checkbox (оформленный переключатель): кликают по его подписи
  const proxyFor = (input) => {
    const candidates = [...(input.labels || []), input.closest('label'), ...labelledBy(input)];
    return candidates.find((c) => c && visible(c) && !chosen.has(c)) || null;
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

  // 1. кандидаты; target — что нумеруем и по чему кликают: сам элемент или подпись скрытого переключателя
  const insideChosen = (node) => {
    for (let p = node.parentElement; p; p = p.parentElement) if (chosen.has(p)) return true;
    return false;
  };
  const candidates = [];
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (el.closest('#' + BOX)) continue;
    const kind = kindOf(el);
    let target = el;
    if (!visible(el)) {
      if (kind !== 'radio' && kind !== 'checkbox') continue;
      target = proxyFor(el);
      if (!target) continue;
    }
    // ссылка внутри кнопки и т.п. — нумеруем внешний элемент
    if (chosen.has(target) || insideChosen(target)) continue;
    chosen.add(target);
    candidates.push({ el, kind, target });
  }

  // 2. номер — только тому, во что можно кликнуть: перекрытое другим слоем (окно поверх, свёрнутое меню
  // под страницей) или вне видимой области — без номера. Страницу проходим окнами по высоте экрана
  const W = innerWidth;
  const H = innerHeight;
  const x0 = scrollX;
  const y0 = scrollY;
  const maxY = Math.max(0, document.documentElement.scrollHeight - H);
  const MAX_VIEWS = 50;
  const center = (t) => {
    const r = t.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  };
  // за краем прокручиваемого блока: его прокрутит кликер, elementFromPoint тут не судья
  const clipped = (t, cx, cy) => {
    for (let p = t.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (/auto|scroll|hidden/.test(s.overflowX + ' ' + s.overflowY)) {
        const r = p.getBoundingClientRect();
        if (cx < r.left || cx >= r.right || cy < r.top || cy >= r.bottom) return true;
      }
    }
    return false;
  };
  // в центре — сам элемент, его потомок или его подпись (оформленный переключатель)
  const owns = (c, hit) => !!hit && (hit === c.target || c.target.contains(hit) ||
    [...(c.el.labels || []), c.el.closest('label'), ...labelledBy(c.el)].some((l) => l && l.contains(hit)));

  const reachable = new Set();
  const covered = [];
  let pending = candidates;
  let views = 0;
  for (let y = 0; pending.length && views < MAX_VIEWS; y += H) {
    const yy = Math.min(y, maxY);
    window.scrollTo(x0, yy);
    views += 1;
    pending = pending.filter((c) => {
      const [cx, cy] = center(c.target);
      if (clipped(c.target, cx, cy)) {
        reachable.add(c);
        return false;
      }
      if (cy < 0 || cy >= H) return true;  // не в этом окне — ждём следующего
      if (cx < 0 || cx >= W) return false;  // за краем страницы по горизонтали
      if (owns(c, document.elementFromPoint(cx, cy))) reachable.add(c);
      else covered.push(c);
      return false;
    });
    if (yy >= maxY) break;
  }
  // очень длинная страница: непроверенные оставляем как были; страница пройдена целиком — остаток вне видимой области
  if (views >= MAX_VIEWS) pending.forEach((c) => reachable.add(c));
  // перекрытые — ещё раз в середине экрана: у края окна их могла закрыть «липкая» шапка или подвал
  for (const c of covered) {
    const r = c.target.getBoundingClientRect();
    window.scrollTo(x0, Math.min(maxY, Math.max(0, r.top + scrollY + r.height / 2 - H / 2)));
    const [cx, cy] = center(c.target);
    if (cy >= 0 && cy < H && cx >= 0 && cx < W && owns(c, document.elementFromPoint(cx, cy))) reachable.add(c);
  }
  window.scrollTo(x0, y0);

  // 3. номера и бейджи
  const elements = [];
  let n = 0;
  for (const c of candidates) {
    if (!reachable.has(c)) continue;
    const { el, kind, target } = c;
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
      nav_item: navItem(el),
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
