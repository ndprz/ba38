// Zone de signature tactile (doigt/stylet/souris). Usage :
//   const pad = initSignaturePad(canvas); pad.reset(); pad.estVide(); pad.toDataURL();
function initSignaturePad(canvas) {
  const ctx = canvas.getContext('2d');
  let dessine = false, signe = false;

  function reset() {
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    canvas.width = r.width * 2; canvas.height = r.height * 2;
    ctx.setTransform(2, 0, 0, 2, 0, 0);
    ctx.lineWidth = 2.5; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#000';
    signe = false;
  }
  function pos(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  canvas.addEventListener('pointerdown', e => {
    dessine = true; canvas.setPointerCapture(e.pointerId);
    const p = pos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y);
    ctx.lineTo(p.x + 0.1, p.y + 0.1); ctx.stroke(); signe = true;
  });
  canvas.addEventListener('pointermove', e => {
    if (!dessine) return;
    const p = pos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); signe = true;
  });
  ['pointerup', 'pointercancel'].forEach(ev =>
    canvas.addEventListener(ev, () => { dessine = false; }));

  return { reset, estVide: () => !signe, toDataURL: () => canvas.toDataURL('image/png') };
}
