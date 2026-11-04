/* Shared clipboard support for HTTPS and the documented LAN HTTP deployment. */
async function copyPlainText(text) {
  const value = String(text == null ? '' : text);
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch (_) {
      // Browser permission can be denied even when the API exists.
    }
  }
  const active = document.activeElement;
  const input = document.createElement('textarea');
  input.value = value;
  input.style.cssText = 'position:fixed;opacity:0;left:0;top:0;';
  input.setAttribute('readonly', '');
  document.body.appendChild(input);
  try {
    input.select();
    input.setSelectionRange(0, input.value.length);
    if (!document.execCommand('copy')) throw new Error('Copierea nu este disponibilă în acest browser.');
  } finally {
    input.remove();
    if (active && typeof active.focus === 'function') active.focus({preventScroll: true});
  }
}
