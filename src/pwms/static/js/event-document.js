/**
 * Workflow detail page: the Events tab's SharePoint document browser.
 *
 * Choosing "Use this document" writes that file's SharePoint URL into the
 * event's document field. The browser is loaded and re-rendered by HTMX, so the
 * handler is delegated from `document`; the field id is read from the picker
 * root rather than hard-coded.
 */
((document) => {
  "use strict";

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-event-document-use]");
    if (!button) {
      return;
    }
    const picker = button.closest("[data-event-document-picker]");
    if (!picker) {
      return;
    }
    const field = document.getElementById(picker.dataset.documentField);
    if (!field) {
      return;
    }

    field.value = button.dataset.eventDocumentUse;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));

    const chosen = document.querySelector("[data-event-document-chosen]");
    if (chosen) {
      chosen.textContent =
        `Selected “${button.dataset.eventDocumentName}”.`;
    }
  });
})(document);
