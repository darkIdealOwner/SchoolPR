// Аудит вёрстки: ищет элементы, которые выходят за границы экрана или своей карточки.
//
// Как запустить: войти на сайт, открыть консоль браузера (F12), вставить этот файл целиком, затем:
//   const r = await layoutAudit(['/home/', '/contracts/', '/contracts/add/'], [320, 375, 768, 1366]);
//   console.table(r.filter(x => !x.ok));
//
// Страница скачивается и вставляется во фрейм заданной ширины (srcdoc — так не мешает защита X-Frame-Options).
// Блоки с намеренной горизонтальной прокруткой (overflow-x: auto/hidden) не считаются ошибкой.
async function layoutAudit(urls, widths) {
  const results = [];
  const frame = document.createElement('iframe');
  frame.style.cssText = 'position:fixed;left:0;top:0;height:900px;border:0;z-index:99999;background:#fff';
  document.body.appendChild(frame);

  const load = async (url) => {
    const html = await (await fetch(url, { credentials: 'same-origin' })).text();
    await new Promise((resolve) => {
      frame.onload = () => setTimeout(resolve, 200);
      frame.srcdoc = html.replace('<head>', `<head><base href="${location.origin}${url}">`);
    });
  };
  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (el.classList.length) s += '.' + [...el.classList].slice(0, 2).join('.');
    const txt = (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 30);
    return txt ? `${s} «${txt}»` : s;
  };

  for (const width of widths) {
    frame.style.width = width + 'px';
    for (const url of urls) {
      await load(url);
      const doc = frame.contentDocument;
      const win = frame.contentWindow;
      const vw = doc.documentElement.clientWidth;
      const problems = [];
      if (doc.documentElement.scrollWidth > vw + 1) {
        problems.push(`страница шире экрана: ${doc.documentElement.scrollWidth}px > ${vw}px`);
      }
      const seen = new Set();
      for (const el of doc.body.querySelectorAll('*')) {
        const r = el.getBoundingClientRect();
        const s = win.getComputedStyle(el);
        if (!(r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.position !== 'fixed')) continue;
        let p = el.parentElement;
        let inScroll = false;
        while (p && p !== doc.body) {
          if (win.getComputedStyle(p).overflowX !== 'visible') { inScroll = true; break; }
          p = p.parentElement;
        }
        if (inScroll) continue;
        if (r.right > vw + 1 || r.left < -1) {
          const key = 'v' + describe(el);
          if (!seen.has(key)) { seen.add(key); problems.push(`за краем экрана: ${describe(el)}`); }
          continue;
        }
        const parent = el.parentElement;
        if (parent && parent !== doc.body) {
          const pr = parent.getBoundingClientRect();
          const ps = win.getComputedStyle(parent);
          const boxed = ps.backgroundColor !== 'rgba(0, 0, 0, 0)' || parseFloat(ps.borderRightWidth) > 0;
          if (boxed && r.right > pr.right + 1.5 && !['TD', 'TH', 'TR'].includes(el.tagName)) {
            const key = 'p' + describe(el);
            if (!seen.has(key)) {
              seen.add(key);
              problems.push(`вылезает из «${describe(parent)}» на ${Math.round(r.right - pr.right)}px: ${describe(el)}`);
            }
          }
        }
      }
      results.push({ width, url, ok: problems.length === 0, problems: problems.slice(0, 6) });
    }
  }
  frame.remove();
  return results;
}
