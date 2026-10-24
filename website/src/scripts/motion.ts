/**
 * Attribute-driven motion. Everything reads without JS and with reduced motion.
 *   data-reveal            fade/slide in when on screen (data-reveal="scale" for frames), --d for a delay
 *   data-count="42"        count up to the number when on screen (data-decimals / data-suffix)
 *   data-draw              gets .is-in when on screen, for a component's own CSS
 *   data-copy="text"       copies the text, then shows "Copied" on the button
 *   data-search-open       opens the docs search (Pagefind, loaded on first use)
 *   video[data-autoplay]   plays muted while on screen, pauses off screen
 * The body gets .scrolled once the page leaves the top (the navbar's glass).
 */
const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
document.documentElement.classList.add('js');

const onScroll = () => document.body.classList.toggle('scrolled', window.scrollY > 12);
window.addEventListener('scroll', onScroll, { passive: true });
onScroll();

const io = new IntersectionObserver(
  (entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const el = e.target as HTMLElement;
      el.classList.add('is-in');
      if (el.dataset.count) countUp(el);
      io.unobserve(el);
    }
  },
  { rootMargin: '0px 0px -8% 0px', threshold: 0.12 },
);
document.querySelectorAll<HTMLElement>('[data-reveal], [data-count], [data-draw]').forEach((el) => io.observe(el));

function countUp(el: HTMLElement) {
  const target = Number(el.dataset.count);
  const decimals = Number(el.dataset.decimals ?? 0);
  const suffix = el.dataset.suffix ?? '';
  const fmt = (n: number) => n.toFixed(decimals) + suffix;
  if (reduce) return void (el.textContent = fmt(target));
  const start = performance.now();
  const tick = (now: number) => {
    const t = Math.min(1, (now - start) / 1400);
    el.textContent = fmt(target * (1 - Math.pow(1 - t, 4)));
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// Videos play only while visible (and never with reduced motion: the poster shows instead).
const videos = new IntersectionObserver(
  (entries) => {
    for (const e of entries) {
      const v = e.target as HTMLVideoElement;
      if (e.isIntersecting) v.play().catch(() => {});
      else v.pause();
    }
  },
  { threshold: 0.25 },
);
if (!reduce) document.querySelectorAll<HTMLVideoElement>('video[data-autoplay]').forEach((v) => videos.observe(v));

document.querySelectorAll<HTMLButtonElement>('[data-copy]').forEach((b) =>
  b.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(b.dataset.copy ?? '');
      const label = b.querySelector('[data-copy-label]');
      if (!label) return;
      const before = label.textContent;
      label.textContent = 'Copied';
      setTimeout(() => (label.textContent = before), 1600);
    } catch {
      /* clipboard blocked: the text is on screen to copy by hand */
    }
  }),
);

// Docs search: Pagefind's UI, loaded the first time it's opened (⌘K / Ctrl+K or the button).
let searchReady: Promise<void> | null = null;
function loadSearch(): Promise<void> {
  if (searchReady) return searchReady;
  searchReady = new Promise((resolve, reject) => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/_pagefind/pagefind-ui.css';
    document.head.append(css);
    const s = document.createElement('script');
    s.src = '/_pagefind/pagefind-ui.js';
    s.onload = () => {
      // @ts-expect-error global from pagefind-ui.js
      new window.PagefindUI({ element: '#search-ui', showSubResults: true, showImages: false, resetStyles: false, autofocus: true });
      resolve();
    };
    s.onerror = reject;
    document.head.append(s);
  });
  return searchReady;
}
const dialog = document.getElementById('search') as HTMLDialogElement | null;
async function openSearch() {
  if (!dialog) return;
  dialog.showModal();
  try {
    await loadSearch();
    (dialog.querySelector('input') as HTMLInputElement | null)?.focus();
  } catch {
    const ui = dialog.querySelector('#search-ui');
    if (ui) ui.textContent = 'Search is available on the built site (bun run build).';
  }
}
document.querySelectorAll('[data-search-open]').forEach((b) => b.addEventListener('click', openSearch));
window.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    openSearch();
  }
});
