/**
 * Workflow detail page: the Actions -> Share modal and Actions -> Print.
 *
 * Everything is bound to `document` (event delegation) so it keeps working
 * whatever HTMX swaps in, and the page needs no per-element wiring.
 *
 *   Share  - copy the record link; add internal users from the HTMX search
 *            results into the recipients box; send the email over fetch so the
 *            outcome (including any "cannot open the link" warning) shows in
 *            place.
 *   Print  - print the record's formal document (the same standalone HTML the
 *            Document menu downloads) through a hidden iframe, so the reader gets
 *            the filed-style print layout rather than the screen UI.
 */
((document) => {
  "use strict";

  const FORM_ID = "workflow-share-form";
  const RESULT_ID = "workflow-share-result";
  const RECIPIENTS_NAME = "recipients";

  // -- helpers ----------------------------------------------------------------
  function splitAddresses(text) {
    return text
      .split(/[\s,;]+/)
      .map((address) => address.trim())
      .filter(Boolean);
  }

  function resetResult(box) {
    box.className = "d-none";
    box.replaceChildren();
  }

  function showResult(box, kind, lines) {
    resetResult(box);
    const list = document.createElement("ul");
    list.className = "mb-0 ps-3";
    for (const line of lines) {
      const item = document.createElement("li");
      item.textContent = line;
      list.append(item);
    }
    box.append(list);
    box.className = `alert alert-${kind}`;
  }

  function errorLines(payload) {
    const lines = [];
    if (payload && payload.error) {
      lines.push(payload.error);
    }
    if (payload && payload.errors) {
      for (const field of Object.values(payload.errors)) {
        for (const entry of field) {
          lines.push(entry.message || String(entry));
        }
      }
    }
    return lines.length ? lines : ["The email could not be sent."];
  }

  // -- link: copy ---------------------------------------------------------------
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("#share-link-copy");
    if (!button) {
      return;
    }
    const input = document.getElementById("share-link");
    try {
      await navigator.clipboard.writeText(input.value);
    } catch {
      input.select();
      document.execCommand("copy");
    }
    button.textContent = "Copied";
    window.setTimeout(() => (button.textContent = "Copy"), 2000);
  });

  // -- email: pick internal recipients ---------------------------------------------
  document.addEventListener("click", (event) => {
    const option = event.target.closest("[data-share-recipient]");
    if (!option) {
      return;
    }
    const form = document.getElementById(FORM_ID);
    const field = form && form.elements[RECIPIENTS_NAME];
    if (!field) {
      return;
    }
    const address = option.dataset.shareRecipient;
    const current = splitAddresses(field.value);
    if (!current.some((entry) => entry.toLowerCase() === address.toLowerCase())) {
      current.push(address);
      field.value = current.join(", ");
    }
    // Clear the search so the next colleague can be looked up straight away.
    const search = form.elements.search;
    if (search) {
      search.value = "";
    }
    document.getElementById("share-recipient-results").replaceChildren();
    field.focus();
  });

  // Enter in the search box looks people up; it must not send the email.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.id === "share-recipient-search") {
      event.preventDefault();
    }
  });

  // A reopened modal starts without the last send's outcome.
  document.addEventListener("show.bs.modal", (event) => {
    if (event.target.id !== "modal-share") {
      return;
    }
    const box = document.getElementById(RESULT_ID);
    if (box) {
      resetResult(box);
    }
  });

  // -- email: send ------------------------------------------------------------------
  async function submitShare(form) {
    const box = document.getElementById(RESULT_ID);
    const submit = form.querySelector('button[type="submit"]');
    resetResult(box);
    submit.disabled = true;

    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: {
          Accept: "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.success) {
        showResult(box, "danger", errorLines(payload));
        return;
      }
      const lines = [
        `Email sent to ${payload.sent} recipient${payload.sent === 1 ? "" : "s"}.`,
        ...(payload.warnings || []),
      ];
      showResult(box, payload.warnings && payload.warnings.length ? "warning" : "success", lines);
      form.elements[RECIPIENTS_NAME].value = "";
    } catch (error) {
      showResult(box, "danger", [`The email could not be sent: ${error}`]);
    } finally {
      submit.disabled = false;
    }
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form instanceof HTMLFormElement && form.id === FORM_ID) {
      event.preventDefault();
      submitShare(form);
    }
  });

  // -- print ------------------------------------------------------------------------
  /**
   * Print the formal document without leaving the page.
   *
   * The document route answers with an attachment disposition and the site sends
   * X-Frame-Options, so the URL cannot be framed directly. Fetching the HTML and
   * loading it as `srcdoc` sidesteps both: the markup is self-contained (inline
   * CSS, embedded logo).
   */
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-workflow-print]");
    if (!button) {
      return;
    }
    button.disabled = true;
    try {
      const response = await fetch(button.dataset.printUrl, {
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const html = await response.text();

      const frame = document.createElement("iframe");
      frame.setAttribute("aria-hidden", "true");
      frame.style.cssText =
        "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden;";
      frame.addEventListener("load", () => {
        frame.contentWindow.focus();
        frame.contentWindow.print();
        // Give the print dialog time to read the document before it is removed.
        window.setTimeout(() => frame.remove(), 60000);
      });
      frame.srcdoc = html;
      document.body.append(frame);
    } catch (error) {
      window.alert(`The document could not be prepared for printing: ${error}`);
    } finally {
      button.disabled = false;
    }
  });
})(document);
