/** Tailwind config — replaces the hand-written `!important` colour shim.
 *
 * The Play CDN can't read a config, so app.css used to re-declare Tailwind's
 * palette with !important to point it at our CSS variables. That was an
 * allowlist: any colour class not listed silently escaped the theme.
 * Declaring the colours here makes Tailwind emit them natively instead.
 *
 * backgroundColor / textColor / borderColor are extended SEPARATELY on purpose:
 * the shim gave e.g. gray-800 one value as a background (--surface-2) and
 * another as a border (--border), which a single `colors` entry can't express.
 */
module.exports = {
  // The JS MUST be here: page markup is built in JS and contains classes that
  // appear nowhere in the templates (bg-indigo-600 / bg-green-400 / bg-gray-600
  // / text-gray-400 for the sidebar connectors, and the per-page renderers in
  // static/pages/). Leave a file out and the purge silently strips its styling.
  content: [
    './app/templates/**/*.html',
    './app/static/app.js',
    './app/static/pages/*.js',
  ],
  theme: {
    extend: {
      backgroundColor: {
        gray: {
          950: 'var(--bg)',
          900: 'var(--surface)',
          800: 'var(--surface-2)',
          600: 'var(--text-3)',        // connector "off" dot
        },
        green:  { 400: 'var(--positive)' },   // connector "on" dot
        indigo: {
          600: 'var(--chrome)',
          500: 'var(--chrome-hover)',
          900: 'rgba(42,45,62,0.5)',          // integration badge
        },
        blue:   { 900: 'rgba(59,130,246,0.15)' },
        purple: { 900: 'rgba(139,92,246,0.15)' },
        orange: { 900: 'rgba(249,115,22,0.15)' },
      },
      textColor: {
        chrome: 'var(--chrome-text)',   // for elements sitting on --chrome
        gray: {
          100: 'var(--text-1)',
          200: 'var(--text-1)',
          300: 'var(--text-2)',
          400: 'var(--text-2)',
          500: 'var(--text-3)',
        },
        green:  { 400: 'var(--positive)' },
        red:    { 400: 'var(--negative)' },
        indigo: { 200: 'var(--text-2)', 300: 'var(--text-2)' },
        yellow: { 400: '#FBBF24' },
        blue:   { 200: '#93C5FD' },
        purple: { 200: '#C4B5FD' },
        orange: { 200: '#FED7AA' },
      },
      borderColor: {
        gray:   { 800: 'var(--border)', 700: 'var(--border-strong)' },
        yellow: { 800: 'rgba(251,191,36,0.3)' },
      },
    },
  },
  plugins: [],
};
