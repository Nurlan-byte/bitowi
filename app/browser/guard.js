// Защита шага извлечения: блокирует кнопки сдачи, проверки и «Показать ответ».
// Подключается к Playwright MCP через --init-script только при извлечении; при сдаче не используется.
(() => {
  if (window.__bitowiGuard) return;
  window.__bitowiGuard = true;

  const BLOCK = /(отправ|сдать|провер|заверш|сохран|показать ответ|сброс|submit|check|finish|save|show answer|reveal|reset)/i;
  const ALLOW = /(начать|продолж|далее|назад|следующ|предыдущ|войти|start|attempt quiz|continue|next|previous|back|log ?in|sign ?in)/i;
  const CLICKABLE = 'button, input[type=submit], input[type=button], input[type=image], a, [role=button]';

  const label = (el) =>
    // name не берём: у Moodle «Завершить попытку» name="next"
    [el.innerText, el.value, el.getAttribute && el.getAttribute('aria-label'), el.title]
      .filter(Boolean).join(' ').trim();

  const forbidden = (el) => {
    if (!el) return false;
    const text = label(el);
    if (el.matches && el.matches('.submit, .show, .reset, .check, .save, [data-bitowi-forbidden]')) return !ALLOW.test(text) || BLOCK.test(text);
    return BLOCK.test(text) && !ALLOW.test(text);
  };

  const block = (event, el, why) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (el && el.setAttribute) el.setAttribute('data-bitowi-blocked', '1');
    console.warn('[bitowi-guard] заблокировано: ' + why);
  };

  document.addEventListener('click', (event) => {
    const el = event.target && event.target.closest ? event.target.closest(CLICKABLE) : null;
    if (forbidden(el)) block(event, el, label(el));
  }, true);

  document.addEventListener('submit', (event) => {
    const sub = event.submitter;
    if (!sub || forbidden(sub)) block(event, sub || event.target, sub ? label(sub) : 'отправка формы без кнопки');
  }, true);

  // form.submit() не порождает событие submit — запрещаем его на этом шаге
  HTMLFormElement.prototype.submit = function () {
    console.warn('[bitowi-guard] заблокировано: form.submit()');
  };
})();
