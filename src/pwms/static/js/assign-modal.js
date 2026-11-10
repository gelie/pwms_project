/**
 * "Assign" modal on a workflow detail page.
 *
 * The modal holds a search picker (see htmx-lookup.js). It starts empty every
 * time it opens, so a person picked and then abandoned is never submitted by
 * mistake, and the search box takes focus so the reader can type straight away.
 */
(() => {
  "use strict";

  const modal = document.getElementById("modal-assign");
  if (!modal) {
    return;
  }

  modal.addEventListener("show.bs.modal", () => {
    modal.querySelector('input[type="hidden"][name="assigned_to"]').value = "";
    modal.querySelector('input[type="text"]').value = "";
    modal.querySelector(".lookup-results").classList.add("d-none");
  });

  modal.addEventListener("shown.bs.modal", () => {
    modal.querySelector('input[type="text"]').focus();
  });
})();
