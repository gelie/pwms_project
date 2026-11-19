/**
 * Workflow detail page: enlarge the state-machine diagram.
 *
 * The Diagram tab shows the generated SVG at thumbnail size; clicking the
 * diagram or its tab opens the same image in a full-screen, transparent overlay
 * with the page blurred behind it. The X, a click on the backdrop, or Escape
 * closes it again.
 *
 * Bound to `document` (event delegation), so it keeps working if the tab or the
 * diagram is ever swapped in by HTMX.
 */
((document) => {
  "use strict";

  const OVERLAY_ID = "diagram-lightbox";
  const BODY_CLASS = "diagram-lightbox-open";

  //: The trigger that opened the overlay, so focus can return to it on close.
  let lastFocused = null;

  function setOpen(open) {
    const overlay = document.getElementById(OVERLAY_ID);
    if (!overlay || overlay.hidden === !open) {
      return;
    }
    overlay.hidden = !open;
    document.body.classList.toggle(BODY_CLASS, open);

    if (open) {
      lastFocused = document.activeElement;
      const close = overlay.querySelector("[data-diagram-lightbox-close]");
      if (close) {
        close.focus();
      }
    } else if (lastFocused) {
      lastFocused.focus();
      lastFocused = null;
    }
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-diagram-lightbox]")) {
      setOpen(true);
      return;
    }

    const overlay = document.getElementById(OVERLAY_ID);
    if (!overlay || overlay.hidden) {
      return;
    }
    // Clicks on the image itself stay put; only the X or the backdrop close.
    if (event.target === overlay || event.target.closest("[data-diagram-lightbox-close]")) {
      setOpen(false);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setOpen(false);
    }
  });
})(document);
