// Apply the persisted theme before first paint without requiring CSP unsafe-inline.
const theme = localStorage.getItem('apdl-admin:theme')
if (
  theme === 'dark' ||
  (theme !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches)
) {
  document.documentElement.classList.add('dark')
}
