/**
 * "Set deadline" modal on the workflow register.
 *
 * One modal serves every row. The row's button carries the record it is for in
 * data attributes; when the modal opens they are copied into the form, and the
 * date box is cleared so a previous row's date is never submitted by mistake.
 */
(() => {
  "use strict";

  const modal = document.getElementById("modal-set-deadline");
  if (!modal) {
    return;
  }

  modal.addEventListener("show.bs.modal", (event) => {
    const button = event.relatedTarget;
    if (!button) {
      return;
    }
    modal.querySelector("#set-deadline-content-type").value =
      button.dataset.deadlineContentType || "";
    modal.querySelector("#set-deadline-object-id").value =
      button.dataset.deadlineObjectId || "";
    modal.querySelector("#set-deadline-title").textContent =
      button.dataset.deadlineTitle || "";
    modal.querySelector("#set-deadline-date").value = "";
  });

  modal.addEventListener("shown.bs.modal", () => {
    modal.querySelector("#set-deadline-date").focus();
  });
})();
