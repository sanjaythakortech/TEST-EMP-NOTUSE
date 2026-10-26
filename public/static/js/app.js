function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  sidebar.classList.toggle('open');
  document.body.classList.toggle('sidebar-open');
}

function togglePassword(inputId, button) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const icon = button.querySelector('i');
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
  icon.className = show ? 'bi bi-eye-slash' : 'bi bi-eye';
}

document.addEventListener('click', (event) => {
  const sidebar = document.getElementById('sidebar');
  const menu = document.querySelector('.mobile-menu');
  if (window.innerWidth <= 900 && sidebar?.classList.contains('open') && !sidebar.contains(event.target) && !menu?.contains(event.target)) {
    sidebar.classList.remove('open');
    document.body.classList.remove('sidebar-open');
  }
});

setTimeout(() => {
  document.querySelectorAll('.app-alert:not(.sticky):not(.alert-warning)').forEach((el) => {
    if (typeof bootstrap !== 'undefined') bootstrap.Alert.getOrCreateInstance(el).close();
  });
}, 4500);

// Confirm popup: <form data-confirm="text" data-confirm-title="Title" data-confirm-btn="Label">
document.addEventListener('submit', (event) => {
  const form = event.target;
  if (!form.dataset || !form.dataset.confirm || form.dataset.confirmed) return;
  event.preventDefault();
  const modalEl = document.getElementById('confirmModal');
  if (!modalEl || typeof bootstrap === 'undefined') { if (window.confirm(form.dataset.confirm)) form.submit(); return; }
  document.getElementById('confirmTitle').textContent = form.dataset.confirmTitle || 'Are you sure?';
  document.getElementById('confirmText').textContent = form.dataset.confirm;
  const ok = document.getElementById('confirmOk');
  ok.textContent = form.dataset.confirmBtn || 'Confirm';
  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  ok.onclick = () => { form.dataset.confirmed = '1'; modal.hide(); form.submit(); };
  modal.show();
});

// Prevent double submits: disable the button and show a spinner while the request runs.
document.addEventListener('submit', (event) => {
  const form = event.target;
  if (event.defaultPrevented || (form.method || '').toLowerCase() !== 'post') return;
  const btn = form.querySelector('button[type=submit], button:not([type])');
  if (!btn) return;
  setTimeout(() => {
    btn.dataset.label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Please wait…';
  }, 0);
});
window.addEventListener('pageshow', () => {
  document.querySelectorAll('button[data-label]').forEach((b) => { b.innerHTML = b.dataset.label; b.disabled = false; });
});

// Instant search: <input data-filter=".css-selector"> hides non-matching rows/cards while typing.
document.addEventListener('input', (event) => {
  const input = event.target;
  if (!input.dataset || !input.dataset.filter) return;
  const q = input.value.trim().toLowerCase();
  document.querySelectorAll(input.dataset.filter).forEach((el) => { el.hidden = !!q && !el.textContent.toLowerCase().includes(q); });
});
