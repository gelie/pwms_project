/**
 * Generic copy-to-clipboard button.
 *
 *   <button type="button" data-copy-text="value to copy">
 *     <span data-copy-label>Copy</span>
 *   </button>
 *
 * On click the text is copied and the label briefly reads "Copied".
 */
(() => {
  "use strict";

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-text]");
    if (!button) {
      return;
    }
    const text = button.dataset.copyText;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Insecure origin or permission denied: fall back to a hidden textarea.
      const field = document.createElement("textarea");
      field.value = text;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      document.execCommand("copy");
      field.remove();
    }
    const label = button.querySelector("[data-copy-label]");
    if (!label) {
      return;
    }
    const original = label.dataset.copyOriginal || label.textContent;
    label.dataset.copyOriginal = original;
    label.textContent = "Copied";
    window.clearTimeout(Number(button.dataset.copyTimer));
    button.dataset.copyTimer = window.setTimeout(() => {
      label.textContent = original;
    }, 2000);
  });
})();
