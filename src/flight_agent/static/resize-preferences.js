const $ = (id) => document.getElementById(id);

export function initializeColumnResize(compact) {
  const key = "flight-agent-column-widths";
  const defaults = {left: 260, right: 340};
  const minimum = {left: 200, right: 240};
  const centerMinimum = 480;
  const handles = {left: $("left-resizer"), right: $("right-resizer")};
  let preferred = {...defaults};
  let widths = {...defaults};
  let drag = null;
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && [saved.left, saved.right].every((value) => Number.isFinite(value) && value > 0)) {
      preferred = {left: saved.left, right: saved.right};
    }
  } catch {
    // Layout preferences must not prevent the task UI from starting.
  }

  function applyWidths() {
    if (compact.matches) return;
    const total = document.documentElement.clientWidth;
    widths.left = Math.round(Math.max(minimum.left, Math.min(
      preferred.left, total - centerMinimum - minimum.right,
    )));
    widths.right = Math.round(Math.max(minimum.right, Math.min(
      preferred.right, total - centerMinimum - widths.left,
    )));
    for (const side of ["left", "right"]) {
      document.body.style.setProperty(`--${side}-column-width`, `${widths[side]}px`);
      const handle = handles[side];
      handle.setAttribute("aria-valuemin", side === "left" ? minimum.left : widths.left + centerMinimum);
      handle.setAttribute("aria-valuemax", side === "left"
        ? total - widths.right - centerMinimum : total - minimum.right);
      handle.setAttribute("aria-valuenow", side === "left" ? widths.left : total - widths.right);
      handle.setAttribute("aria-valuetext", `${side === "left" ? "左" : "右"}栏 ${widths[side]} 像素`);
    }
  }

  function saveWidths() {
    try {
      localStorage.setItem(key, JSON.stringify(preferred));
    } catch {
      // Resizing still works when browser preference storage is unavailable.
    }
  }

  function adjust(side, value) {
    const other = side === "left" ? "right" : "left";
    preferred = {...widths, [side]: Math.max(minimum[side], Math.min(
      Math.round(value), document.documentElement.clientWidth - centerMinimum - widths[other],
    ))};
    applyWidths();
  }

  function finish(cancel = false) {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (cancel) preferred = previous.preferred;
    document.body.classList.remove("is-resizing-columns");
    if (previous.handle.hasPointerCapture(previous.pointerId)) {
      previous.handle.releasePointerCapture(previous.pointerId);
    }
    applyWidths();
    if (!cancel) saveWidths();
  }

  function reset() {
    finish(true);
    preferred = {...defaults};
    applyWidths();
    saveWidths();
  }

  for (const [side, handle] of Object.entries(handles)) {
    handle.addEventListener("pointerdown", (event) => {
      if (compact.matches || event.button !== 0 || !event.isPrimary || drag) return;
      event.preventDefault();
      drag = {handle, pointerId: event.pointerId, x: event.clientX,
        width: widths[side], preferred: {...preferred}};
      handle.setPointerCapture(event.pointerId);
      handle.focus({preventScroll: true});
      document.body.classList.add("is-resizing-columns");
    });
    handle.addEventListener("pointermove", (event) => {
      if (drag?.handle !== handle || drag.pointerId !== event.pointerId) return;
      adjust(side, drag.width + (side === "left" ? 1 : -1) * (event.clientX - drag.x));
    });
    for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
      handle.addEventListener(name, (event) => {
        if (drag?.handle === handle && drag.pointerId === event.pointerId) finish(name !== "pointerup");
      });
    }
    handle.addEventListener("dblclick", reset);
    handle.addEventListener("keydown", (event) => {
      if (compact.matches) return;
      if (event.key === "Escape") {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Enter") {
        event.preventDefault();
        reset();
      } else if (["ArrowLeft", "ArrowRight"].includes(event.key) && !drag) {
        event.preventDefault();
        const delta = (event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 50 : 10);
        adjust(side, widths[side] + (side === "left" ? delta : -delta));
        saveWidths();
      }
    });
  }
  window.addEventListener("resize", () => { finish(true); applyWidths(); });
  window.addEventListener("blur", () => finish(true));
  applyWidths();
}
